# SPDX-License-Identifier: BSD-2-Clause

from contextlib import contextmanager
from itertools  import chain
from re         import search

from vcd        import VCDWriter
from vcd.gtkw   import GTKWSave

from ..hdl      import *
from ..hdl.ast  import SignalDict
from ._base     import *
from ._pyclock  import PyClockProcess
from ._pycoro   import PyCoroProcess
from ._pyrtl    import _FragmentCompiler

__all__ = (
	'PySimEngine',
)


class _NameExtractor:
	def __init__(self):
		self.names = SignalDict()

	def __call__(self, fragment, *, hierarchy = ('bench', 'top',)):
		def add_signal_name(signal):
			hierarchical_signal_name = (*hierarchy, signal.name)
			if signal not in self.names:
				self.names[signal] = {hierarchical_signal_name}
			else:
				self.names[signal].add(hierarchical_signal_name)

		for domain_name, domain_signals in fragment.drivers.items():
			if domain_name is not None:
				domain = fragment.domains[domain_name]
				add_signal_name(domain.clk)
				if domain.rst is not None:
					add_signal_name(domain.rst)

		for statement in fragment.statements:
			for signal in statement._lhs_signals() | statement._rhs_signals():
				if not isinstance(signal, (ClockSignal, ResetSignal)):
					add_signal_name(signal)

		for subfragment_index, (subfragment, subfragment_name) in enumerate(fragment.subfragments):
			if subfragment_name is None:
				subfragment_name = f'U${subfragment_name}'
			self(subfragment, hierarchy=(*hierarchy, subfragment_name))

		return self.names


class _VCDWriter:
	@staticmethod
	def decode_to_vcd(signal, value):
		return signal.decoder(value).expandtabs().replace(' ', '_')

	def __init__(self, fragment, *, vcd_file, gtkw_file = None, traces = ()):
		if isinstance(vcd_file, str):
			vcd_file = open(vcd_file, 'wt')
		if isinstance(gtkw_file, str):
			gtkw_file = open(gtkw_file, 'wt')

		self.vcd_vars = SignalDict()
		self.vcd_file = vcd_file
		self.vcd_writer = vcd_file and VCDWriter(self.vcd_file,
			timescale='1 ps', comment = 'Generated by Torii')

		self.gtkw_names = SignalDict()
		self.gtkw_file = gtkw_file
		self.gtkw_save = gtkw_file and GTKWSave(self.gtkw_file)

		self.traces = []

		signal_names = _NameExtractor()(fragment)

		trace_names = SignalDict()
		for trace in traces:
			if trace not in signal_names:
				trace_names[trace] = { ('bench', trace.name) }
			self.traces.append(trace)

		if self.vcd_writer is None:
			return

		for signal, names in chain(signal_names.items(), trace_names.items()):
			if signal.decoder:
				var_type = 'string'
				var_size = 1
				var_init = self.decode_to_vcd(signal, signal.reset)
			else:
				var_type = 'wire'
				var_size = signal.width
				var_init = signal.reset

			for (*var_scope, var_name) in names:
				if search(r'[ \t\r\n]', var_name):
					raise NameError(f'Signal \'{".".join(var_scope)}.{var_name}\' contains a whitespace character')

				suffix = None
				while True:
					try:
						if suffix is None:
							var_name_suffix = var_name
						else:
							var_name_suffix = f'{var_name}${suffix}'
						if signal not in self.vcd_vars:
							vcd_var = self.vcd_writer.register_var(
								scope = var_scope, name = var_name_suffix,
								var_type = var_type, size = var_size, init = var_init)
							self.vcd_vars[signal] = vcd_var
						else:
							self.vcd_writer.register_alias(
								scope=var_scope, name = var_name_suffix,
								var=self.vcd_vars[signal])
						break
					except KeyError:
						suffix = (suffix or 0) + 1

				if signal not in self.gtkw_names:
					self.gtkw_names[signal] = (*var_scope, var_name_suffix)

	def update(self, timestamp, signal, value):
		vcd_var = self.vcd_vars.get(signal)
		if vcd_var is None:
			return

		if signal.decoder:
			var_value = self.decode_to_vcd(signal, value)
		else:
			var_value = value
		self.vcd_writer.change(vcd_var, timestamp, var_value)

	def close(self, timestamp):
		if self.vcd_writer is not None:
			self.vcd_writer.close(timestamp)

		if self.gtkw_save is not None:
			self.gtkw_save.dumpfile(self.vcd_file.name)
			self.gtkw_save.dumpfile_size(self.vcd_file.tell())

			self.gtkw_save.treeopen('top')
			for signal in self.traces:
				if len(signal) > 1 and not signal.decoder:
					suffix = f'[{len(signal) - 1}:0]'
				else:
					suffix = ''
				self.gtkw_save.trace('.'.join(self.gtkw_names[signal]) + suffix)

		if self.vcd_file is not None:
			self.vcd_file.close()
		if self.gtkw_file is not None:
			self.gtkw_file.close()


class _Timeline:
	def __init__(self):
		self.now = 0
		self.deadlines = dict()

	def reset(self):
		self.now = 0
		self.deadlines.clear()

	def at(self, run_at, process):
		assert process not in self.deadlines
		self.deadlines[process] = run_at

	def delay(self, delay_by, process):
		if delay_by is None:
			run_at = self.now
		else:
			run_at = self.now + delay_by
		self.at(run_at, process)

	def advance(self):
		nearest_processes = set()
		nearest_deadline = None
		for process, deadline in self.deadlines.items():
			if deadline is None:
				if nearest_deadline is not None:
					nearest_processes.clear()
				nearest_processes.add(process)
				nearest_deadline = self.now
				break
			elif nearest_deadline is None or deadline <= nearest_deadline:
				assert deadline >= self.now
				if nearest_deadline is not None and deadline < nearest_deadline:
					nearest_processes.clear()
				nearest_processes.add(process)
				nearest_deadline = deadline

		if not nearest_processes:
			return False

		for process in nearest_processes:
			process.runnable = True
			del self.deadlines[process]
		self.now = nearest_deadline

		return True


class _PySignalState(BaseSignalState):
	__slots__ = ('signal', 'curr', 'next', 'waiters', 'pending')

	def __init__(self, signal, pending):
		self.signal = signal
		self.pending = pending
		self.waiters = dict()
		self.curr = self.next = signal.reset

	def set(self, value):
		if self.next == value:
			return
		self.next = value
		self.pending.add(self)

	def commit(self):
		if self.curr == self.next:
			return False
		self.curr = self.next

		awoken_any = False
		for process, trigger in self.waiters.items():
			if trigger is None or trigger == self.curr:
				process.runnable = awoken_any = True
		return awoken_any


class _PySimulation(BaseSimulation):
	def __init__(self):
		self.timeline = _Timeline()
		self.signals  = SignalDict()
		self.slots    = []
		self.pending  = set()

	def reset(self):
		self.timeline.reset()
		for signal, index in self.signals.items():
			self.slots[index].curr = self.slots[index].next = signal.reset
		self.pending.clear()

	def get_signal(self, signal):
		try:
			return self.signals[signal]
		except KeyError:
			index = len(self.slots)
			self.slots.append(_PySignalState(signal, self.pending))
			self.signals[signal] = index
			return index

	def add_trigger(self, process, signal, *, trigger = None):
		index = self.get_signal(signal)
		assert (process not in self.slots[index].waiters or
				self.slots[index].waiters[process] == trigger)
		self.slots[index].waiters[process] = trigger

	def remove_trigger(self, process, signal):
		index = self.get_signal(signal)
		assert process in self.slots[index].waiters
		del self.slots[index].waiters[process]

	def wait_interval(self, process, interval):
		self.timeline.delay(interval, process)

	def commit(self, changed=None):
		converged = True
		for signal_state in self.pending:
			if signal_state.commit():
				converged = False
		if changed is not None:
			changed.update(self.pending)
		self.pending.clear()
		return converged


class PySimEngine(BaseEngine):
	def __init__(self, fragment):
		self._state = _PySimulation()
		self._timeline = self._state.timeline

		self._fragment = fragment
		self._processes = _FragmentCompiler(self._state)(self._fragment)
		self._vcd_writers = []

	def add_coroutine_process(self, process, *, default_cmd):
		self._processes.add(PyCoroProcess(self._state, self._fragment.domains, process,
										  default_cmd = default_cmd))

	def add_clock_process(self, clock, *, phase, period):
		self._processes.add(PyClockProcess(self._state, clock,
										   phase = phase, period = period))

	def reset(self):
		self._state.reset()
		for process in self._processes:
			process.reset()

	def _step(self):
		changed = set() if self._vcd_writers else None

		# Performs the two phases of a delta cycle in a loop:
		converged = False
		while not converged:
			# 1. eval: run and suspend every non-waiting process once, queueing signal changes
			for process in self._processes:
				if process.runnable:
					process.runnable = False
					process.run()

			# 2. commit: apply every queued signal change, waking up any waiting processes
			converged = self._state.commit(changed)

		for vcd_writer in self._vcd_writers:
			for signal_state in changed:
				vcd_writer.update(self._timeline.now,
					signal_state.signal, signal_state.curr)

	def advance(self):
		self._step()
		self._timeline.advance()
		return any(not process.passive for process in self._processes)

	@property
	def now(self):
		return self._timeline.now

	@contextmanager
	def write_vcd(self, *, vcd_file, gtkw_file, traces):
		vcd_writer = _VCDWriter(self._fragment,
			vcd_file = vcd_file, gtkw_file = gtkw_file, traces = traces)
		try:
			self._vcd_writers.append(vcd_writer)
			yield
		finally:
			vcd_writer.close(self._timeline.now)
			self._vcd_writers.remove(vcd_writer)
