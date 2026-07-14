import struct
import subprocess
import sys
from sys import argv

# ---------------------------------------------------------------------------
# PSC -> x86-64 assembly compiler
#
# This reads a .psc source file, emits an x86-64 (AT&T syntax) assembly file,
# assembles + links it into a native executable via the system C compiler
# (clang/gcc, which also gives us libc/libm for printf/scanf/sin/cos/pow/floor),
# and then runs the resulting binary.
#
# All PSC variables are compiled to 64-bit doubles so a single code path
# covers both integer and floating point math (mirrors the original
# interpreter, where `/` and math.* already produced floats).
# ---------------------------------------------------------------------------

libraries = []
variables = set()


def is_number(token):
	try:
		float(token)
		return True
	except ValueError:
		return False


def var_label(name):
	return "V_" + name


class AsmBuilder:
	def __init__(self):
		self.body = []
		self.consts = []
		self._const_n = 0

	def emit(self, line):
		self.body.append(line)

	def const(self, value):
		if not hasattr(self, "_const_cache"):
			self._const_cache = {}
		bits = struct.unpack("<Q", struct.pack("<d", float(value)))[0]
		if bits in self._const_cache:
			return self._const_cache[bits]
		label = "LC%d" % self._const_n
		self._const_n += 1
		self.consts.append("%s: .quad 0x%016x" % (label, bits))
		self._const_cache[bits] = label
		return label

	def load_value(self, token, xmm):
		variables.add(token) if not is_number(token) else None
		if is_number(token):
			label = self.const(token)
			self.emit("\tmovsd %s(%%rip), %%%s" % (label, xmm))
		else:
			variables.add(token)
			self.emit("\tmovsd %s(%%rip), %%%s" % (var_label(token), xmm))

	def store(self, name, xmm):
		variables.add(name)
		self.emit("\tmovsd %%%s, %s(%%rip)" % (xmm, var_label(name)))


def compile_cmd(asm, line):
	line = line.strip()
	if line == "":
		return
	parts = line.split(" ")
	cmd = parts[0]
	args = parts[1:]

	binops = {"add": "addsd", "sub": "subsd", "mul": "mulsd", "div": "divsd"}

	if cmd == "set":
		asm.load_value(args[1], "xmm0")
		asm.store(args[0], "xmm0")

	elif cmd in binops:
		variables.add(args[0])
		asm.emit("\tmovsd %s(%%rip), %%xmm0" % var_label(args[0]))
		asm.load_value(args[1], "xmm1")
		asm.emit("\t%s %%xmm1, %%xmm0" % binops[cmd])
		asm.store(args[0], "xmm0")

	elif cmd == "pow":
		variables.add(args[0])
		asm.emit("\tmovsd %s(%%rip), %%xmm0" % var_label(args[0]))
		asm.load_value(args[1], "xmm1")
		asm.emit("\tcall _pow")
		asm.store(args[0], "xmm0")

	elif cmd == "input":
		variables.add(args[0])
		fmt = asm.cstring("%lf")
		asm.emit("\tlea %s(%%rip), %%rdi" % fmt)
		asm.emit("\tlea %s(%%rip), %%rsi" % var_label(args[0]))
		asm.emit("\txor %eax, %eax")
		asm.emit("\tcall _scanf")

	elif cmd == "print":
		fmt = asm.cstring("%.16g\\n")
		asm.load_value(args[0], "xmm0")
		asm.emit("\tlea %s(%%rip), %%rdi" % fmt)
		asm.emit("\tmov $1, %al")
		asm.emit("\tcall _printf")

	elif "math" in libraries and cmd in ("floor", "sin", "cos"):
		variables.add(args[0])
		asm.emit("\tmovsd %s(%%rip), %%xmm0" % var_label(args[0]))
		asm.emit("\tcall _%s" % cmd)
		asm.store(args[0], "xmm0")

	# unknown/blank commands are silently ignored, matching the original
	# interpreter's behaviour for stray separators.


# cstring interning lives on AsmBuilder too, added here to keep the class
# definition above focused on numeric constants.
def _cstring(self, text):
	if not hasattr(self, "_strings"):
		self._strings = {}
		self._string_n = 0
	if text in self._strings:
		return self._strings[text]
	label = "LS%d" % self._string_n
	self._string_n += 1
	self.consts.append('%s: .asciz "%s"' % (label, text))
	self._strings[text] = label
	return label


AsmBuilder.cstring = _cstring


def compile_source(text):
	statements = text.replace("\n", "").split(";")
	asm = AsmBuilder()

	for line in statements:
		line = line.strip()
		if line.startswith("#import "):
			for library in line[len("#import "):].split(" "):
				if library == "graphics.psl":
					libraries.append("graphics")
				elif library == "math.psl":
					libraries.append("math")
		else:
			compile_cmd(asm, line)

	out = []
	out.append("\t.data")
	out.extend("\t" + c for c in asm.consts)
	out.append("")
	out.append("\t.bss")
	for name in sorted(variables):
		out.append("\t.lcomm %s, 8" % var_label(name))
	out.append("")
	out.append("\t.text")
	out.append("\t.globl _main")
	out.append("_main:")
	out.append("\tpush %rbp")
	out.append("\tmov %rsp, %rbp")
	out.extend(asm.body)
	out.append("\txor %eax, %eax")
	out.append("\tleave")
	out.append("\tret")
	out.append("")
	return "\n".join(out)


def main():
	if len(argv) < 2:
		print("usage: python3 compiler.py file.psc [-S] [-o output]")
		sys.exit(1)

	src_path = argv[1]
	keep_asm_only = "-S" in argv[2:]

	out_name = None
	if "-o" in argv[2:]:
		out_name = argv[argv.index("-o") + 1]
	if out_name is None:
		out_name = src_path.rsplit(".", 1)[0]

	with open(src_path, "r") as f:
		source = f.read()

	asm_text = compile_source(source)
	asm_path = out_name + ".s"
	with open(asm_path, "w") as f:
		f.write(asm_text)
	print("wrote %s" % asm_path)

	if keep_asm_only:
		return

	subprocess.run(["clang", asm_path, "-lm", "-o", out_name], check=True)
	print("compiled %s" % out_name)

	subprocess.run(["./" + out_name], check=True)


if __name__ == "__main__":
	main()
