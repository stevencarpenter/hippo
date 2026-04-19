import Foundation
import Darwin

struct DaemonSocketClient: Sendable {
    let socketURL: URL

    init(configClient: ConfigClient = ConfigClient()) {
        self.socketURL = configClient.loadDataDirectory().appendingPathComponent("daemon.sock")
    }

    init(socketURL: URL) {
        self.socketURL = socketURL
    }

    func isResponsive(timeout: TimeInterval = 1) -> Bool {
        guard FileManager.default.fileExists(atPath: socketURL.path) else {
            return false
        }

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            return false
        }
        defer { Darwin.close(fd) }

        var timeoutValue = timeval(tv_sec: Int(timeout), tv_usec: Int32((timeout - floor(timeout)) * 1_000_000))
        withUnsafePointer(to: &timeoutValue) { pointer in
            _ = Darwin.setsockopt(
                fd,
                SOL_SOCKET,
                SO_RCVTIMEO,
                pointer,
                socklen_t(MemoryLayout<timeval>.size)
            )
            _ = Darwin.setsockopt(
                fd,
                SOL_SOCKET,
                SO_SNDTIMEO,
                pointer,
                socklen_t(MemoryLayout<timeval>.size)
            )
        }

        var address = sockaddr_un()
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        address.sun_family = sa_family_t(AF_UNIX)

        let path = socketURL.path.utf8CString
        let capacity = MemoryLayout.size(ofValue: address.sun_path)
        guard path.count <= capacity else {
            return false
        }

        withUnsafeMutableBytes(of: &address.sun_path) { rawBuffer in
            rawBuffer.initializeMemory(as: CChar.self, repeating: 0)
            path.withUnsafeBytes { sourceBuffer in
                guard let destinationBase = rawBuffer.baseAddress, let sourceBase = sourceBuffer.baseAddress else {
                    return
                }
                destinationBase.copyMemory(from: sourceBase, byteCount: path.count)
            }
        }

        return withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
                Darwin.connect(fd, sockaddrPointer, socklen_t(MemoryLayout<sockaddr_un>.size)) == 0
            }
        }
    }
}
