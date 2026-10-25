//! TUN fd 的原始读写。fd 的所有权仍归 Kotlin（`ParcelFileDescriptor`），
//! 这里只用 `libc::read/write`，绝不 close。

use std::io;
use std::os::raw::{c_int, c_void};

pub struct TunDevice {
    fd: c_int,
}

impl TunDevice {
    /// 由 Java 侧传来的 fd 构造；`fd < 0` 视为无效。
    pub fn from_fd(fd: c_int) -> Option<TunDevice> {
        if fd < 0 {
            None
        } else {
            Some(TunDevice { fd })
        }
    }

    pub fn fd(&self) -> c_int {
        self.fd
    }

    /// 阻塞读一个 IP 包；返回 0 表示对端关闭（应停止）。
    pub fn read_packet(&self, buf: &mut [u8]) -> io::Result<usize> {
        loop {
            let n = unsafe {
                libc::read(
                    self.fd,
                    buf.as_mut_ptr() as *mut c_void,
                    buf.len() as libc::size_t,
                )
            };
            if n >= 0 {
                return Ok(n as usize);
            }
            let e = io::Error::last_os_error();
            match e.raw_os_error() {
                Some(libc::EINTR) => continue,
                Some(libc::EAGAIN) => {
                    std::thread::sleep(std::time::Duration::from_millis(2));
                    continue;
                }
                _ => return Err(e),
            }
        }
    }

    /// 写一个 IP 包进 TUN。
    pub fn write_packet(&self, buf: &[u8]) -> io::Result<usize> {
        loop {
            let n = unsafe {
                libc::write(
                    self.fd,
                    buf.as_ptr() as *const c_void,
                    buf.len() as libc::size_t,
                )
            };
            if n >= 0 {
                return Ok(n as usize);
            }
            let e = io::Error::last_os_error();
            match e.raw_os_error() {
                Some(libc::EINTR) | Some(libc::EAGAIN) => continue,
                _ => return Err(e),
            }
        }
    }
}

/// 让 socket 走真实网卡（不回到 TUN），对应上游的 `VPN socket protection failed`。
/// 由调用方提供一个 `protect(fd) -> bool` 回调（Kotlin 侧转 `VpnService.protect`）。
pub fn protect_socket(fd: c_int, protect: &dyn Fn(c_int) -> bool) -> Result<(), String> {
    if protect(fd) {
        Ok(())
    } else {
        Err("VPN socket protection failed".to_string())
    }
}

pub fn socket_fd(sock: &std::net::UdpSocket) -> c_int {
    use std::os::unix::io::AsRawFd;
    sock.as_raw_fd()
}
