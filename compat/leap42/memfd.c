/* glibc 2.22 lacks this wrapper; Leap 42.3's kernel implements the syscall.
 * Preload only in the T3 wrapper, never system-wide. */
#define _GNU_SOURCE
#include <sys/syscall.h>
#include <unistd.h>
#include <errno.h>

int memfd_create(const char *name, unsigned int flags) {
#ifdef SYS_memfd_create
    return (int)syscall(SYS_memfd_create, name, flags);
#else
    errno = ENOSYS;
    return -1;
#endif
}
