// Shared by the SSH ownership and signal-boundary probes. Darwin's ps joins argv
// with spaces, so shell parsing cannot prove paths or empty argument boundaries.
export const REMOTE_PROCESS_ARGV_PY = `
def process_argv(pid):
 import ctypes,os,shlex,subprocess,sys
 try:
  if sys.platform=="darwin":
   sysctl=ctypes.CDLL(None,use_errno=True).sysctl
   sysctl.argtypes=[ctypes.POINTER(ctypes.c_int),ctypes.c_uint,ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p,ctypes.c_size_t]
   size=ctypes.c_size_t(os.sysconf("SC_ARG_MAX"))
   buffer=ctypes.create_string_buffer(size.value)
   mib=(ctypes.c_int*3)(1,49,pid) # CTL_KERN, KERN_PROCARGS2, pid
   if sysctl(mib,3,buffer,ctypes.byref(size),None,0)!=0:return []
   raw=buffer.raw[:size.value]
   width=ctypes.sizeof(ctypes.c_int)
   argc=int.from_bytes(raw[:width],sys.byteorder,signed=True)
   start=raw.index(b"\\0",width)+1 # saved executable path, then alignment padding
   parts=raw[start:].lstrip(b"\\0").split(b"\\0")
   if argc<1 or len(parts)<=argc:return []
   return [part.decode("utf-8","surrogateescape") for part in parts[:argc]]
  if sys.platform.startswith("linux"):
   raw=open(f"/proc/{pid}/cmdline","rb").read()
   if not raw.endswith(b"\\0"):return []
   return [part.decode("utf-8","surrogateescape") for part in raw.split(b"\\0")[:-1]]
  return shlex.split(subprocess.check_output(["ps","-ww","-o","command=","-p",str(pid)],text=True).strip())
 except (OSError,ValueError,AttributeError,subprocess.CalledProcessError):return []
`.trim()
