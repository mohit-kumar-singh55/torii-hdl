# SPDX-License-Identifier: BSD-2-Clause

from typing             import Tuple, Union

from ..tools.yosys      import find_yosys, YosysError
from ..hdl              import ir, ast
from .                  import rtlil

__all__ = (
	'YosysError',
	'convert',
	'convert_fragment',
)


def _convert_rtlil_text(
	rtlil_text: str, *, strip_internal_attrs: bool = False, write_verilog_opts: Tuple[str] = ()
) -> str:
	# this version requirement needs to be synchronized with the one in setup.py!
	yosys = find_yosys(lambda ver: ver >= (0, 10))
	yosys_version = yosys.version()

	script = []
	script.append(f'read_ilang <<rtlil\n{rtlil_text}\nrtlil')
	if yosys_version >= (0, 17):
		script.append('proc -nomux -norom')
	else:
		script.append('proc -nomux')
	script.append('memory_collect')

	if strip_internal_attrs:
		attr_map = []
		attr_map.append('-remove generator')
		attr_map.append('-remove top')
		attr_map.append('-remove src')
		attr_map.append('-remove torii.hierarchy')
		attr_map.append('-remove torii.decoding')
		script.append(f'attrmap {" ".join(attr_map)}')
		script.append(f'attrmap -modattr {" ".join(attr_map)}')

	script.append(f'write_verilog -norename {" ".join(write_verilog_opts)}')

	return yosys.run(['-q', '-'], '\n'.join(script),
		# At the moment, Yosys always shows a warning indicating that not all processes can be
		# translated to Verilog. We carefully emit only the processes that *can* be translated, and
		# squash this warning. Once Yosys' write_verilog pass is fixed, we should remove this.
		ignore_warnings = True
	)


def convert_fragment(*args, strip_internal_attrs: bool = False, **kwargs) -> Tuple[str, ast.SignalDict]:
	rtlil_text, name_map = rtlil.convert_fragment(*args, **kwargs)
	return (_convert_rtlil_text(rtlil_text, strip_internal_attrs = strip_internal_attrs), name_map)


def convert(
	elaboratable: Union[ir.Fragment, ir.Elaboratable], name: str = 'top', platform = None, *, ports,
	emit_src: bool = True, strip_internal_attrs: bool = False, **kwargs
) -> str:

	fragment = ir.Fragment.get(elaboratable, platform).prepare(ports = ports, **kwargs)
	verilog_text, _ = convert_fragment(fragment, name, emit_src = emit_src, strip_internal_attrs = strip_internal_attrs)
	return verilog_text
