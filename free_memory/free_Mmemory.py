import os,psutil,gc,ctypes
def current_rss_mb():
    return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024

def trim_memory(var):
    print("after alloc:", current_rss_mb())
    del var
    try:
        gc.collect()  # Clear Python's own cyclic garbage first
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
        print("after dealloc:", current_rss_mb())
    except (OSError, AttributeError):
        pass  # Not Linux or no glibc
