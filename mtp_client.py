"""
Pure-Python MTP (Media Transfer Protocol) client using pyusb + libusb.
Works with basic/feature phones that present an MTP interface (Class 6, Sub 1, Proto 1).
"""
import struct
import os
import logging
import threading
from typing import Optional, List, Tuple, Dict

try:
    import usb.core
    import usb.util
    PYUSB_AVAILABLE = True
except ImportError:
    PYUSB_AVAILABLE = False

log = logging.getLogger('mtp')

# ---------------------------------------------------------------------------
# MTP Operation Codes
# ---------------------------------------------------------------------------
OP_GET_DEVICE_INFO    = 0x1001
OP_OPEN_SESSION       = 0x1002
OP_CLOSE_SESSION      = 0x1003
OP_GET_STORAGE_IDS    = 0x1004
OP_GET_STORAGE_INFO   = 0x1005
OP_GET_OBJECT_HANDLES = 0x1007
OP_GET_OBJECT_INFO    = 0x1008
OP_GET_OBJECT         = 0x1009
OP_DELETE_OBJECT      = 0x100B
OP_SEND_OBJECT_INFO   = 0x100C
OP_SEND_OBJECT        = 0x100D
OP_COPY_OBJECT             = 0x101A  # may not be supported
OP_MOVE_OBJECT             = 0x1019  # may not be supported
OP_SET_OBJECT_PROP_VALUE   = 0x9804

# MTP Object Property Codes
PROP_OBJECT_FILENAME       = 0xDC07

# MTP Response Codes
RESP_OK               = 0x2001
RESP_ERROR            = 0x2002
RESP_SESSION_NOT_OPEN = 0x2003
RESP_ACCESS_DENIED    = 0x200F
RESP_OBJECT_NOT_FOUND = 0x2009
RESP_DEVICE_BUSY      = 0x2019
RESP_PARAM_NOT_SUPPORTED = 0x2006

# MTP Container Types
CONTAINER_COMMAND  = 1
CONTAINER_DATA     = 2
CONTAINER_RESPONSE = 3
CONTAINER_EVENT    = 4

# MTP Object Formats
FORMAT_UNDEFINED  = 0x3000
FORMAT_FOLDER     = 0x3001
FORMAT_TEXT       = 0x3004
FORMAT_MP3        = 0x3009
FORMAT_AVI        = 0x300A
FORMAT_MPEG       = 0x300B
FORMAT_JPEG       = 0x3801
FORMAT_PNG        = 0x3808
FORMAT_BMP        = 0x3804

# Special handles
HANDLE_ROOT     = 0xFFFFFFFF  # root parent handle
STORAGE_ALL     = 0xFFFFFFFF  # all storages

# ObjectInfo field sizes  (all little-endian)
# StorageID u32, ObjectFormat u16, ProtectionStatus u16,
# ObjectCompressedSize u32,
# ThumbFormat u16, ThumbCompressedSize u32,
# ThumbPixWidth u32, ThumbPixHeight u32,
# ImagePixWidth u32, ImagePixHeight u32, ImageBitDepth u32,
# ParentObject u32, AssociationType u16, AssociationDesc u32,
# SequenceNumber u32,
# Filename (MTP string), DateCreated (MTP string), DateModified (MTP string), Keywords (MTP string)
OBJINFO_FIXED_FMT = '<IHHIHIIIIIIIHII'
OBJINFO_FIXED_SIZE = struct.calcsize(OBJINFO_FIXED_FMT)  # 52 bytes


class MTPError(Exception):
    pass


class MTPCancelled(MTPError):
    """Raised when a transfer is aborted via its cancel callback."""
    pass


def is_disconnect_error(exc) -> bool:
    """True if an exception (or error message) means the USB device vanished."""
    if PYUSB_AVAILABLE and isinstance(exc, usb.core.USBError):
        if exc.errno == 19:  # ENODEV
            return True
        if getattr(exc, 'backend_error_code', None) == -4:  # LIBUSB_ERROR_NO_DEVICE
            return True
    msg = str(exc)
    return any(m in msg for m in (
        'No such device', 'LIBUSB_ERROR_NO_DEVICE', 'Errno 19', 'disconnected'))


class MTPDevice:
    def __init__(self):
        self.dev = None
        self.ep_in: int = 0x81
        self.ep_out: int = 0x01
        self.intf_num: int = 0
        self._txid = 1
        self._session_id = 1
        self._session_open = False
        self._storage_ids: Optional[List[int]] = None
        # Serializes all USB/MTP transactions. The GUI issues directory
        # listings and file transfers from separate QThreads; without this
        # lock their command/response containers would interleave on the
        # shared bulk endpoints and corrupt the protocol stream. Reentrant
        # so high-level helpers can call locked primitives.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def find_mtp_devices(self) -> List[Dict]:
        """Return list of dicts describing connected MTP devices."""
        if not PYUSB_AVAILABLE:
            return []
        results = []
        try:
            devs = list(usb.core.find(find_all=True))
        except Exception:
            return []
        for d in devs:
            try:
                for cfg in d:
                    for intf in cfg:
                        if intf.bInterfaceClass == 6 and intf.bInterfaceSubClass == 1:
                            try:
                                mfr = usb.util.get_string(d, d.iManufacturer) if d.iManufacturer else ''
                                prod = usb.util.get_string(d, d.iProduct) if d.iProduct else ''
                            except Exception:
                                mfr, prod = '', ''
                            name = f"{mfr} {prod}".strip() or f"Device {hex(d.idVendor)}:{hex(d.idProduct)}"
                            results.append({
                                'name': name,
                                'vendor_id': d.idVendor,
                                'product_id': d.idProduct,
                                'serial': d.iSerialNumber,
                            })
            except Exception:
                pass
        return results

    def connect(self, vendor_id: int = None, product_id: int = None) -> Tuple[bool, str]:
        """Find and connect to an MTP device, open session."""
        if not PYUSB_AVAILABLE:
            return False, "pyusb not installed. Run: pip install pyusb"

        # Always wipe stale state so reconnect works cleanly
        self._mark_dead()

        # Find device
        kwargs = {}
        if vendor_id is not None:
            kwargs['idVendor'] = vendor_id
        if product_id is not None:
            kwargs['idProduct'] = product_id

        self.dev = None
        try:
            if kwargs:
                candidates = [usb.core.find(**kwargs)]
            else:
                candidates = list(usb.core.find(find_all=True))

            for d in candidates:
                if d is None:
                    continue
                for cfg in d:
                    for intf in cfg:
                        if intf.bInterfaceClass == 6 and intf.bInterfaceSubClass == 1:
                            self.dev = d
                            self.intf_num = intf.bInterfaceNumber
                            # Find endpoints
                            for ep in intf:
                                direction = usb.util.endpoint_direction(ep.bEndpointAddress)
                                ep_type = usb.util.endpoint_type(ep.bmAttributes)
                                if ep_type == usb.util.ENDPOINT_TYPE_BULK:
                                    if direction == usb.util.ENDPOINT_IN:
                                        self.ep_in = ep.bEndpointAddress
                                    else:
                                        self.ep_out = ep.bEndpointAddress
                            break
                    if self.dev:
                        break
                if self.dev:
                    break
        except Exception as e:
            return False, f"USB error: {e}"

        if not self.dev:
            return False, "No MTP device found. Connect phone and set USB mode to 'File Transfer'."

        # Fully release any previous claim on this device
        try:
            usb.util.release_interface(self.dev, self.intf_num)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

        # Re-acquire the device object freshly
        try:
            self.dev = usb.core.find(idVendor=self.dev.idVendor,
                                     idProduct=self.dev.idProduct)
        except Exception:
            pass
        if self.dev is None:
            return False, "Device disappeared after reconnect attempt."

        # Detach kernel driver if needed (Linux only; silently skipped on macOS)
        try:
            if self.dev.is_kernel_driver_active(self.intf_num):
                self.dev.detach_kernel_driver(self.intf_num)
        except Exception:
            pass

        # Claim interface
        claimed = False
        try:
            usb.util.claim_interface(self.dev, self.intf_num)
            claimed = True
        except Exception as e:
            return False, (f"Cannot claim USB interface: {e}\n\n"
                           "Try:\n"
                           "• Close Image Capture / Photos if open\n"
                           "• Unplug and replug the phone\n"
                           "• Run the app with sudo")

        # Flush any stale data in the device's output buffer
        self._flush_stale_data()

        # Cancel any in-progress MTP operation
        self._send_reset()

        # Open MTP session
        try:
            self._open_session()
            self._session_open = True
            log.debug("Connected %04x:%04x intf=%d ep_in=0x%02x ep_out=0x%02x",
                      self.dev.idVendor, self.dev.idProduct,
                      self.intf_num, self.ep_in, self.ep_out)
            return True, "Connected"
        except Exception as e:
            # Clean up on failure
            try:
                usb.util.release_interface(self.dev, self.intf_num)
            except Exception:
                pass
            return False, f"Could not open MTP session: {e}"

    def disconnect(self):
        if self._session_open:
            try:
                self._close_session()
            except Exception:
                pass
            self._session_open = False
        if self.dev:
            try:
                usb.util.release_interface(self.dev, self.intf_num)
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None
        self._storage_ids = None

    def _mark_dead(self):
        """Force-clears connection state without any USB I/O. Safe to call after device removal."""
        self._session_open = False
        self.dev = None
        self._storage_ids = None

    def ping(self) -> bool:
        """
        Quickly check whether the USB device is still present.
        Does NOT do any MTP-level I/O — just scans the USB bus.
        Returns True if found, False (and marks dead) if gone.
        """
        if not self._session_open or self.dev is None:
            return False
        try:
            found = usb.core.find(idVendor=self.dev.idVendor,
                                  idProduct=self.dev.idProduct)
            if found is None:
                self._mark_dead()
                return False
            return True
        except Exception:
            self._mark_dead()
            return False

    def is_connected(self) -> bool:
        return self._session_open and self.dev is not None

    # ------------------------------------------------------------------
    # Low-level USB/MTP protocol
    # ------------------------------------------------------------------

    def _flush_stale_data(self):
        """Drain pending data on the IN endpoint (stale session or aborted transfer)."""
        for _ in range(8):
            try:
                self.dev.read(self.ep_in, 65536, timeout=200)
            except Exception:
                break

    def _send_reset(self):
        """Send MTP cancel/reset to clear device state."""
        try:
            self.dev.ctrl_transfer(
                0x21,  # bmRequestType: class, interface, host-to-device
                0x64,  # bRequest: Cancel Request
                0x0001,
                self.intf_num,
                b'',
                timeout=1000,
            )
        except Exception:
            pass

    def _recover_from_abort(self):
        """Best-effort reset of the device's data phase after an aborted transfer."""
        if self.dev is None:
            return
        self._send_reset()
        self._flush_stale_data()

    @staticmethod
    def _remove_quiet(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    def _next_txid(self) -> int:
        tid = self._txid
        self._txid = (self._txid % 0xFFFFFFFE) + 1
        return tid

    def _send_data_container(self, op_code: int, txid: int, payload: bytes):
        length = 12 + len(payload)
        hdr = struct.pack('<IHHI', length, CONTAINER_DATA, op_code, txid)
        # Send in chunks up to 512 bytes (or 64KB for high-speed)
        chunk_size = 65536
        full = hdr + payload
        for i in range(0, len(full), chunk_size):
            self.dev.write(self.ep_out, full[i:i + chunk_size], timeout=30000)
        # If payload is an exact multiple of wMaxPacketSize, send ZLP
        if len(full) % 512 == 0:
            self.dev.write(self.ep_out, b'', timeout=5000)

    def _recv_container(self, timeout: int = 5000) -> Tuple[int, int, int, bytes]:
        """Read one MTP container. Returns (ctype, code, txid, payload)."""
        try:
            raw = bytes(self.dev.read(self.ep_in, 65536, timeout=timeout))
        except Exception as e:
            if is_disconnect_error(e):
                self._mark_dead()
                raise MTPError("Phone disconnected (USB error: device not found)")
            raise
        if len(raw) < 12:
            raise MTPError(f"Short MTP container: {len(raw)} bytes")
        length, ctype, code, txid = struct.unpack_from('<IHHI', raw)
        payload = raw[12:]

        # If the declared length is more than what we got, read more
        to_read = length - len(raw)
        while to_read > 0:
            try:
                chunk = bytes(self.dev.read(self.ep_in, min(to_read, 65536), timeout=timeout))
            except Exception:
                break
            if not chunk:
                break
            payload += chunk
            to_read -= len(chunk)

        return ctype, code, txid, payload

    def _operation(self, op_code: int, params: List[int] = None,
                   send_payload: bytes = None, recv_data: bool = True,
                   timeout: int = 5000) -> Tuple[int, bytes]:
        """
        Full MTP operation:
          1. Send command
          2. Optionally send data
          3. Optionally receive data
          4. Receive response
        Returns (response_code, data_payload).
        """
        params = params or []
        with self._lock:
            txid = self._next_txid()
            log.debug("op 0x%04X txid=%d params=%s", op_code, txid,
                      [hex(p) for p in params])

            # 1. Send command
            length = 12 + 4 * len(params)
            hdr = struct.pack('<IHHI', length, CONTAINER_COMMAND, op_code, txid)
            body = b''.join(struct.pack('<I', p) for p in params)
            self.dev.write(self.ep_out, hdr + body, timeout=5000)

            # 2. Send data if provided
            if send_payload is not None:
                self._send_data_container(op_code, txid, send_payload)

            # 3. Read data container if expected
            data_payload = b''
            if recv_data:
                ctype, code, _, raw = self._recv_container(timeout=timeout)
                if ctype == CONTAINER_DATA:
                    data_payload = raw
                    # Read the response container next
                    ctype, code, _, _ = self._recv_container(timeout=timeout)
                # else it's directly the response
            else:
                ctype, code, _, _ = self._recv_container(timeout=timeout)

            log.debug("op 0x%04X -> resp 0x%04X (%d data bytes)",
                      op_code, code, len(data_payload))
            return code, data_payload

    def _open_session(self):
        txid = 1  # Session open always uses txid=1
        hdr = struct.pack('<IHHI', 16, CONTAINER_COMMAND, OP_OPEN_SESSION, txid)
        body = struct.pack('<I', self._session_id)
        self.dev.write(self.ep_out, hdr + body, timeout=5000)

        # Read response, skipping any unexpected data containers
        for _ in range(3):
            ctype, code, _, _ = self._recv_container(timeout=5000)
            if ctype == CONTAINER_RESPONSE:
                break
            # Got a data container we didn't expect — drain it and read again
        if code not in (RESP_OK, 0x201E):  # 0x201E = SessionAlreadyOpen
            raise MTPError(f"OpenSession failed: code={hex(code)}")
        self._txid = 2  # after open session, start at 2

    def _close_session(self):
        self._operation(OP_CLOSE_SESSION, recv_data=False, timeout=3000)

    # ------------------------------------------------------------------
    # MTP string parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_mtp_string(data: bytes, offset: int) -> Tuple[str, int]:
        """Parse an MTP string starting at offset. Returns (string, new_offset)."""
        if offset >= len(data):
            return '', offset
        num_chars = data[offset]
        offset += 1
        if num_chars == 0:
            return '', offset
        byte_len = num_chars * 2
        if offset + byte_len > len(data):
            return '', offset + byte_len
        raw = data[offset:offset + byte_len]
        s = raw.decode('utf-16-le', errors='replace').rstrip('\x00')
        return s, offset + byte_len

    @staticmethod
    def _encode_mtp_string(s: str) -> bytes:
        """Encode a Python string as an MTP string."""
        if not s:
            return b'\x00'
        encoded = (s + '\x00').encode('utf-16-le')
        num_chars = len(encoded) // 2
        return bytes([num_chars]) + encoded

    # ------------------------------------------------------------------
    # High-level MTP operations
    # ------------------------------------------------------------------

    def get_storage_ids(self) -> List[int]:
        """Return list of storage IDs (e.g. internal memory, SD card)."""
        code, data = self._operation(OP_GET_STORAGE_IDS)
        if code != RESP_OK:
            raise MTPError(f"GetStorageIDs failed: {hex(code)}")
        count = struct.unpack_from('<I', data)[0]
        ids = list(struct.unpack_from(f'<{count}I', data, 4))
        self._storage_ids = ids
        return ids

    def cached_storage_ids(self) -> Optional[List[int]]:
        """Storage IDs from the last get_storage_ids() call, without USB I/O."""
        return self._storage_ids

    def get_storage_info(self, storage_id: int) -> Dict:
        """Return info dict for a storage."""
        code, data = self._operation(OP_GET_STORAGE_INFO, params=[storage_id])
        if code != RESP_OK:
            raise MTPError(f"GetStorageInfo failed: {hex(code)}")
        # StorageType u16, FilesystemType u16, AccessCapability u16,
        # MaxCapacityL u64, FreeSpaceInBytesL u64, FreeSpaceInObjects u32,
        # StorageDescription (Mtp string), VolumeIdentifier (mtp string)
        offset = 0
        storage_type, fs_type, access = struct.unpack_from('<HHH', data, offset)
        offset += 6
        max_cap, free_bytes = struct.unpack_from('<QQ', data, offset)
        offset += 16
        free_objects = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        desc, offset = self._parse_mtp_string(data, offset)
        vol_id, offset = self._parse_mtp_string(data, offset)
        return {
            'storage_id': storage_id,
            'description': desc or vol_id or f'Storage {hex(storage_id)}',
            'max_capacity': max_cap,
            'free_space': free_bytes,
        }

    def list_dir(self, parent_handle: int = HANDLE_ROOT,
                 storage_id: int = STORAGE_ALL) -> List[Dict]:
        """
        List objects in a directory (MTP parent handle).
        Returns list of dicts: {handle, name, is_dir, size, storage_id}

        Design:
        • We pass the real parent_handle to GetObjectHandles so the device
          filters by directory (most devices respect this even when they
          ignore storage_id).
        • We use STORAGE_ALL so devices that ignore storage_id still return
          all handles for that directory rather than an empty list.
        • We post-filter results by storage_id using the reliable value
          from each object's GetObjectInfo — this removes cross-storage
          duplicates without hiding real subdirectory contents.
        • We do NOT filter by the parent field from ObjectInfo because many
          cheap MTP devices report parent=0 for every object regardless of
          its actual location. Trusting the device's GetObjectHandles
          response for parent-scoping is more reliable.

        NOTE on the parent parameter: per the PTP/MTP spec, GetObjectHandles
        takes parent=0xFFFFFFFF to mean "objects at the root level" and
        parent=0x00000000 to mean "all objects on the device". The UI uses 0
        as its own storage-root sentinel, so it is remapped to HANDLE_ROOT
        (0xFFFFFFFF) here; real folder handles pass through unchanged.
        """
        # Map the UI's storage-root sentinel (0) to the spec's root handle
        effective_parent = HANDLE_ROOT if parent_handle == 0 else parent_handle
        code, data = self._operation(
            OP_GET_OBJECT_HANDLES,
            params=[STORAGE_ALL, 0, effective_parent],
        )
        if code != RESP_OK:
            raise MTPError(f"GetObjectHandles failed: {hex(code)}")
        count = struct.unpack_from('<I', data)[0]
        if count == 0:
            return []
        handles = list(struct.unpack_from(f'<{count}I', data, 4))

        items = []
        seen_handles = set()
        for handle in handles:
            if handle in seen_handles:
                continue
            seen_handles.add(handle)
            try:
                info = self.get_object_info(handle)
            except Exception:
                continue
            # Only filter by storage_id — do NOT filter by parent field
            # (many devices report parent=0 for all objects, which would
            # make every subdirectory appear empty)
            if storage_id != STORAGE_ALL and info['storage_id'] != storage_id:
                continue
            items.append(info)
        return items

    def get_object_info(self, handle: int) -> Dict:
        """Return metadata for a single object."""
        code, data = self._operation(OP_GET_OBJECT_INFO, params=[handle])
        if code != RESP_OK:
            raise MTPError(f"GetObjectInfo failed for handle {handle}: {hex(code)}")
        if len(data) < OBJINFO_FIXED_SIZE:
            raise MTPError(f"GetObjectInfo data too short: {len(data)} bytes")
        fields = struct.unpack_from(OBJINFO_FIXED_FMT, data)
        (storage_id, obj_format, protection, compressed_size,
         thumb_fmt, thumb_size, thumb_w, thumb_h,
         img_w, img_h, img_depth,
         parent_handle, assoc_type, assoc_desc, seq_num) = fields
        offset = OBJINFO_FIXED_SIZE
        filename, offset = self._parse_mtp_string(data, offset)
        date_created, offset = self._parse_mtp_string(data, offset)
        date_modified, offset = self._parse_mtp_string(data, offset)

        is_dir = (obj_format == FORMAT_FOLDER)
        return {
            'handle': handle,
            'name': filename,
            'is_dir': is_dir,
            'size': compressed_size,
            'format': obj_format,
            'parent': parent_handle,
            'storage_id': storage_id,
            'date_modified': date_modified,
        }

    def get_object(self, handle: int, dest_path: str,
                   progress_cb=None, cancel_cb=None) -> bool:
        """
        Download a file from the device to dest_path (streaming, handles large files).

        The file streams to '<dest_path>.part' and is renamed into place on
        success, so a failed or cancelled transfer never leaves a partial
        file that looks complete.

        progress_cb, if given, is called as progress_cb(bytes_done, bytes_total)
        as the file streams in. bytes_total is 0 when the device declares the
        size unknown (objects >4GB), so treat total as a best-effort hint.

        cancel_cb, if given, is polled between chunks; return True to abort
        the transfer (resets the device state, then raises MTPCancelled).
        """
        tmp_path = dest_path + '.part'
        with self._lock:
            if self.dev is None:
                raise MTPError("Phone not connected")
            try:
                self._get_object_streaming(handle, tmp_path, progress_cb, cancel_cb)
            except MTPCancelled:
                self._recover_from_abort()
                self._remove_quiet(tmp_path)
                raise
            except Exception:
                self._remove_quiet(tmp_path)
                raise
        os.replace(tmp_path, dest_path)
        return True

    def _get_object_streaming(self, handle, dest_path, progress_cb, cancel_cb):
        txid = self._next_txid()
        log.debug("GetObject handle=%d -> %s", handle, dest_path)

        # ── Phase 1: Send GetObject command ──
        cmd = struct.pack('<IHHI', 16, CONTAINER_COMMAND, OP_GET_OBJECT, txid)
        cmd += struct.pack('<I', handle)
        self.dev.write(self.ep_out, cmd, timeout=5000)

        # ── Phase 2: Receive data container (stream to file) ──
        CHUNK = 65536
        resp_code = None
        with open(dest_path, 'wb') as f:
            first = True
            remaining = None   # None = device declared the length unknown
            total = 0
            written = 0
            while True:
                if cancel_cb is not None and cancel_cb():
                    raise MTPCancelled("Download cancelled")
                try:
                    raw = bytes(self.dev.read(self.ep_in, CHUNK, timeout=60000))
                except Exception as e:
                    if is_disconnect_error(e):
                        self._mark_dead()
                    raise MTPError(f"GetObject read error: {e}")
                if not raw:
                    break
                if first:
                    if len(raw) < 12:
                        raise MTPError("GetObject: short first packet")
                    length, ctype, code, _ = struct.unpack_from('<IHHI', raw)
                    if ctype == CONTAINER_RESPONSE:
                        if code != RESP_OK:
                            raise MTPError(f"GetObject failed: {hex(code)}")
                        return  # empty file
                    # DATA container. A length of 0xFFFFFFFF means the object
                    # doesn't fit the 32-bit field (>4GB) — stream until the
                    # device sends the trailing response container.
                    if length == 0xFFFFFFFF:
                        total = 0
                        remaining = None
                    else:
                        total = length - 12  # total payload bytes expected
                        remaining = total
                    payload = raw[12:]
                    f.write(payload)
                    written += len(payload)
                    if remaining is not None:
                        remaining -= len(payload)
                    first = False
                else:
                    if remaining is None and len(raw) == 12:
                        # Unknown length: watch for the response container.
                        r_len, r_type, r_code, _ = struct.unpack_from('<IHHI', raw)
                        if r_len == 12 and r_type == CONTAINER_RESPONSE:
                            resp_code = r_code
                            break
                    f.write(raw)
                    written += len(raw)
                    if remaining is not None:
                        remaining -= len(raw)
                if progress_cb is not None:
                    progress_cb(written, total)
                if remaining is not None and remaining <= 0:
                    break

        # ── Phase 3: Read response container (unless already received) ──
        if resp_code is None:
            _, resp_code, _, _ = self._recv_container(timeout=10000)
        if resp_code != RESP_OK:
            raise MTPError(f"GetObject response failed: {hex(resp_code)}")

    def send_object(self, src_path: str, parent_handle: int,
                    storage_id: int = None, progress_cb=None,
                    cancel_cb=None) -> int:
        """
        Upload src_path to the device under parent_handle.
        Returns the new object handle (0 if not returned by device).

        cancel_cb, if given, is polled between chunks; return True to abort
        (resets the device state, deletes the half-written object, then
        raises MTPCancelled).

        MTP upload sequence:
          1. SendObjectInfo  command  → data (ObjectInfo struct) → response (with new handle)
          2. SendObject      command  → data (file bytes)        → response
        """
        with self._lock:
            if self.dev is None:
                raise MTPError("Phone not connected")
            if cancel_cb is not None and cancel_cb():
                raise MTPCancelled("Upload cancelled")
            if storage_id is None:
                ids = self._storage_ids or self.get_storage_ids()
                storage_id = ids[0] if ids else 0x00010001

            filename = os.path.basename(src_path)
            file_size = os.path.getsize(src_path)
            obj_format = FORMAT_UNDEFINED  # generic binary
            log.debug("SendObject %s (%d bytes) parent=%d storage=%s",
                      filename, file_size, parent_handle, hex(storage_id))

            # ── Build ObjectInfo struct ──────────────────────────────────
            objinfo = struct.pack(
                OBJINFO_FIXED_FMT,
                storage_id,     # StorageID
                obj_format,     # ObjectFormat
                0,              # ProtectionStatus
                file_size if file_size < 0xFFFFFFFF else 0xFFFFFFFF,
                0, 0,           # ThumbFormat, ThumbCompressedSize
                0, 0,           # ThumbPixWidth, ThumbPixHeight
                0, 0, 0,        # ImagePixWidth, ImagePixHeight, ImageBitDepth
                parent_handle,  # ParentObject
                0, 0,           # AssociationType, AssociationDesc
                0,              # SequenceNumber
            )
            objinfo += self._encode_mtp_string(filename)
            objinfo += b'\x00'   # DateCreated  (empty MTP string)
            objinfo += b'\x00'   # DateModified (empty MTP string)
            objinfo += b'\x00'   # Keywords     (empty MTP string)

            # ── Phase 1: SendObjectInfo ──────────────────────────────────
            # 1a. Command container
            txid1 = self._next_txid()
            cmd_len = 12 + 4 * 2   # header + 2 params (storage_id, parent_handle)
            cmd = struct.pack('<IHHI', cmd_len, CONTAINER_COMMAND, OP_SEND_OBJECT_INFO, txid1)
            cmd += struct.pack('<II', storage_id, parent_handle)
            self.dev.write(self.ep_out, cmd, timeout=5000)

            # 1b. Data container (ObjectInfo payload)
            data_len = 12 + len(objinfo)
            data_hdr = struct.pack('<IHHI', data_len, CONTAINER_DATA, OP_SEND_OBJECT_INFO, txid1)
            self.dev.write(self.ep_out, data_hdr + objinfo, timeout=10000)

            # 1c. Response container → contains (storage_id, parent_handle, new_handle)
            new_handle = 0
            for _ in range(3):  # allow skipping unexpected DATA echoes
                ctype, code, _, resp_payload = self._recv_container(timeout=10000)
                if ctype == CONTAINER_RESPONSE:
                    break
            if code != RESP_OK:
                raise MTPError(f"SendObjectInfo failed: {hex(code)}")
            # Response params are appended after the 12-byte header
            if len(resp_payload) >= 12:
                new_handle = struct.unpack_from('<I', resp_payload, 8)[0]
            elif len(resp_payload) >= 4:
                new_handle = struct.unpack_from('<I', resp_payload, 0)[0]

            # ── Phase 2: SendObject ──────────────────────────────────────
            try:
                # 2a. Command container (no parameters)
                txid2 = self._next_txid()
                cmd = struct.pack('<IHHI', 12, CONTAINER_COMMAND, OP_SEND_OBJECT, txid2)
                self.dev.write(self.ep_out, cmd, timeout=5000)

                # 2b. Data container header. The length field is 32-bit; for
                # objects >4GB the spec says to send 0xFFFFFFFF and stream on.
                total_len = 12 + file_size
                data_hdr = struct.pack('<IHHI',
                                       total_len if total_len < 0xFFFFFFFF else 0xFFFFFFFF,
                                       CONTAINER_DATA, OP_SEND_OBJECT, txid2)
                self.dev.write(self.ep_out, data_hdr, timeout=5000)

                # 2c. Stream file data in chunks
                CHUNK = 65536
                sent = 0
                with open(src_path, 'rb') as f:
                    while True:
                        if cancel_cb is not None and cancel_cb():
                            raise MTPCancelled("Upload cancelled")
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break
                        self.dev.write(self.ep_out, chunk, timeout=60000)
                        sent += len(chunk)
                        if progress_cb is not None:
                            progress_cb(sent, file_size)

                # 2d. Zero-length packet if total sent was multiple of max-packet-size
                if total_len % 512 == 0:
                    self.dev.write(self.ep_out, b'', timeout=5000)

                # 2e. Response
                ctype, code, _, _ = self._recv_container(timeout=60000)
                if code != RESP_OK:
                    raise MTPError(f"SendObject failed: {hex(code)}")
            except MTPCancelled:
                # Reset the aborted data phase and drop the incomplete object.
                self._recover_from_abort()
                if new_handle:
                    try:
                        self.delete_object(new_handle)
                    except Exception:
                        pass
                raise

            return new_handle

    def delete_object(self, handle: int) -> bool:
        """Delete an object (file or folder) by handle."""
        code, _ = self._operation(OP_DELETE_OBJECT, params=[handle, 0],
                                  recv_data=False)
        if code != RESP_OK:
            raise MTPError(f"DeleteObject failed: {hex(code)}")
        return True

    def rename_object(self, handle: int, new_name: str) -> bool:
        """
        Rename an object on the device using SetObjectPropValue (0x9804).
        Property 0xDC07 = ObjectFileName.
        """
        encoded = self._encode_mtp_string(new_name)
        with self._lock:
            txid = self._next_txid()
            # Command: SetObjectPropValue, params=[handle, property_code]
            cmd_len = 12 + 4 * 2
            cmd = struct.pack('<IHHI', cmd_len, CONTAINER_COMMAND,
                              OP_SET_OBJECT_PROP_VALUE, txid)
            cmd += struct.pack('<II', handle, PROP_OBJECT_FILENAME)
            self.dev.write(self.ep_out, cmd, timeout=5000)
            # Data: the new filename as an MTP string
            data_len = 12 + len(encoded)
            data_hdr = struct.pack('<IHHI', data_len, CONTAINER_DATA,
                                   OP_SET_OBJECT_PROP_VALUE, txid)
            self.dev.write(self.ep_out, data_hdr + encoded, timeout=5000)
            # Response
            ctype, code, _, _ = self._recv_container(timeout=5000)
            if code != RESP_OK:
                raise MTPError(f"Rename failed: {hex(code)}")
            return True

    def move_object(self, handle: int, new_storage_id: int,
                    new_parent_handle: int) -> bool:
        """
        Move an object to a different parent folder using MoveObject (0x1019).
        Many basic MTP devices don't support this — raises MTPError with a
        clear message so the caller can surface it to the user.
        """
        code, _ = self._operation(
            OP_MOVE_OBJECT,
            params=[handle, new_storage_id, new_parent_handle],
            recv_data=False,
            timeout=10000,
        )
        if code == 0x2005:  # OperationNotSupported
            raise MTPError("This phone does not support moving files over MTP.\n"
                           "Workaround: copy the file to Mac, then upload to the new folder.")
        if code != RESP_OK:
            raise MTPError(f"MoveObject failed: {hex(code)}")
        return True

    def create_folder(self, name: str, parent_handle: int,
                      storage_id: int = None) -> int:
        """Create a new folder. Returns new handle (0 if unsupported)."""
        if storage_id is None:
            ids = self._storage_ids or self.get_storage_ids()
            storage_id = ids[0] if ids else 0x00010001

        objinfo = struct.pack(
            OBJINFO_FIXED_FMT,
            storage_id,
            FORMAT_FOLDER,
            0, 0, 0, 0, 0, 0, 0, 0, 0,
            parent_handle,
            0x0001,  # AssociationType = GenericFolder
            0, 0,
        )
        objinfo += self._encode_mtp_string(name)
        objinfo += b'\x00\x00\x00'

        with self._lock:
            # Use raw approach to capture response params
            txid = self._next_txid()
            length = 12 + 4 * 2  # command with 2 params
            hdr = struct.pack('<IHHI', length, CONTAINER_COMMAND, OP_SEND_OBJECT_INFO, txid)
            body = struct.pack('<II', storage_id, parent_handle)
            self.dev.write(self.ep_out, hdr + body, timeout=5000)
            self._send_data_container(OP_SEND_OBJECT_INFO, txid, objinfo)

            # Read possibly the data echo then response
            ctype, code, _, resp_payload = self._recv_container(timeout=5000)
            if ctype == CONTAINER_DATA:
                ctype, code, _, resp_payload = self._recv_container(timeout=5000)

            if code != RESP_OK:
                raise MTPError(f"CreateFolder (SendObjectInfo) failed: {hex(code)}")

            # New handle is in response params (3rd param)
            if len(resp_payload) >= 12:
                new_handle = struct.unpack_from('<I', resp_payload, 8)[0]
            else:
                new_handle = 0
            return new_handle
