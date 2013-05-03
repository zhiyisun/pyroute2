import threading
import select
import struct
import socket
import Queue
import copy
import os
import io
import uuid

from pyroute2.netlink.generic import cmdmsg
from pyroute2.netlink.generic import nlmsg
from pyroute2.netlink.generic import NETLINK_GENERIC


## Netlink message flags values (nlmsghdr.flags)
#
NLM_F_REQUEST = 1    # It is request message.
NLM_F_MULTI = 2    # Multipart message, terminated by NLMSG_DONE
NLM_F_ACK = 4    # Reply with ack, with zero or error code
NLM_F_ECHO = 8    # Echo this request
# Modifiers to GET request
NLM_F_ROOT = 0x100    # specify tree    root
NLM_F_MATCH = 0x200    # return all matching
NLM_F_ATOMIC = 0x400    # atomic GET
NLM_F_DUMP = (NLM_F_ROOT | NLM_F_MATCH)
# Modifiers to NEW request
NLM_F_REPLACE = 0x100    # Override existing
NLM_F_EXCL = 0x200    # Do not touch, if it exists
NLM_F_CREATE = 0x400    # Create, if it does not exist
NLM_F_APPEND = 0x800    # Add to end of list

NLMSG_NOOP = 0x1    # Nothing
NLMSG_ERROR = 0x2    # Error
NLMSG_DONE = 0x3    # End of a dump
NLMSG_OVERRUN = 0x4    # Data lost
NLMSG_MIN_TYPE = 0x10    # < 0x10: reserved control messages
NLMSG_MAX_LEN = 0xffff  # Max message length

IPRCMD_NOOP = 1
IPRCMD_REGISTER = 2
IPRCMD_UNREGISTER = 3
IPRCMD_STOP = 4
IPRCMD_RELOAD = 5


class netlink_error(socket.error):
    def __init__(self, code, msg=None):
        msg = msg or os.strerror(code)
        super(netlink_error, self).__init__(code, msg)
        self.code = code


class marshal(object):
    '''
    Generic marshalling class
    '''

    msg_map = {}

    def __init__(self):
        self.lock = threading.Lock()
        # one marshal instance can be used to parse one
        # message at once
        self.buf = None
        self.msg_map = self.msg_map or {}

    def set_buffer(self, init=b''):
        '''
        Set the buffer and return the data length
        '''
        self.buf = io.BytesIO()
        self.buf.write(init)
        self.buf.seek(0)
        return len(init)

    def parse(self, data):
        '''
        Parse the data in the buffer
        '''
        with self.lock:
            total = self.set_buffer(data)
            offset = 0
            result = []

            while offset < total:
                # pick type and length
                (length, msg_type) = struct.unpack('IH', self.buf.read(6))
                error = None
                if msg_type == NLMSG_ERROR:
                    self.buf.seek(16)
                    code = abs(struct.unpack('i', self.buf.read(4))[0])
                    if code > 0:
                        error = netlink_error(code)

                self.buf.seek(offset)
                msg_class = self.msg_map.get(msg_type, nlmsg)
                msg = msg_class(self.buf)
                msg.decode()
                msg['header']['error'] = error
                self.fix_message(msg)
                offset += msg.length
                result.append(msg)

            return result

    def fix_message(self, msg):
        pass


class netlink_socket(socket.socket):
    '''
    Generic netlink socket
    '''

    def __init__(self, family=NETLINK_GENERIC):
        socket.socket.__init__(self, socket.AF_NETLINK,
                               socket.SOCK_DGRAM, family)
        self.pid = os.getpid()
        self.groups = None

    def bind(self, groups=0):
        self.groups = groups
        socket.socket.bind(self, (self.pid, self.groups))


class server(threading.Thread):
    def __init__(self, url, iothread):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.clients = []
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(url)
        self.socket.listen(10)
        self.iothread = iothread
        self.uuid = uuid.uuid4()
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.control.bind(b'\0%s' % (self.uuid))
        self._rlist = set()
        self._rlist.add(self.control)
        self._rlist.add(self.socket)
        self._stop = False

    def run(self):
        while not self._stop:
            [rlist, wlist, xlist] = select.select(self._rlist, [], [])
            for fd in rlist:
                if fd == self.control:
                    self._stop = True
                    break
                else:
                    (client, addr) = fd.accept()
                    self.iothread.add_client(client)


class iothread(threading.Thread):
    def __init__(self, marshal, send_method=None):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self._rlist = set()
        self._wlist = set()
        self._xlist = set()
        self.send = send_method
        self.marshal = marshal()
        self.listeners = {}
        self.netlink = None
        self.clients = set()
        self.uplinks = set()
        self.uuid = uuid.uuid4()
        self.mirror = False
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.control.bind(b'\0%s' % (self.uuid))
        self._rlist.add(self.control)
        self._stop = False

    def parse(self, data):
        '''
        Parse and enqueue messages. A message can be
        retrieved from netlink socket as well as from a
        remote system, and it should be properly enqueued
        to make it available for netlink.get() method.

        If iothread.mirror is set, all messages will be also
        copied (mirrored) to the default 0 queue. Please
        make sure that 0 queue exists, before setting
        iothread.mirror to True.

        If there is no such queue for received
        sequence_number, leave sequence_number intact, but
        put the message into default 0 queue, if it exists.
        '''

        # TODO: create a hook for custom cmdmsg?

        for msg in self.marshal.parse(data):
            key = msg['header']['sequence_number']
            if key not in self.listeners:
                key = 0
            if self.mirror and key != 0:
                self.listeners[0].put(copy.deepcopy(msg))
            if key in self.listeners:
                self.listeners[key].put(msg)

    def command(self, cmd, v1=0, v2=0):
        msg = cmdmsg(io.BytesIO())
        msg['command'] = cmd
        msg.encode()
        return self.control.sendto(msg.buf.getvalue(),
                                   self.control.getsockname())

    def stop(self):
        return self.command(IPRCMD_STOP)

    def reload(self):
        return self.command(IPRCMD_RELOAD)

    def set_netlink(self, family, groups):
        '''
        [re]set netlink connection and reload I/O cycle.
        '''
        if self.netlink is not None:
            self._rlist.remove(self.netlink)
            self.netlink.close()
        self.netlink = netlink_socket(family)
        self.netlink.bind(groups)
        self._rlist.add(self.netlink)
        self.reload()
        return self.netlink

    def add_uplink(self, url):
        '''
        Add an uplink server to get information from
        '''
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(url)
        self._rlist.add(sock)
        self.uplinks.add(sock)
        self.reload()
        return sock

    def remove_uplink(self, sock):
        self._rlist.remove(sock)
        self.uplinks.remove(sock)
        self.reload()
        return sock

    def add_client(self, sock):
        '''
        Add a client connection. Should not be called
        manually, but only on a client connect.
        '''
        self._rlist.add(sock)
        self._wlist.add(sock)
        self.clients.add(sock)
        self.reload()
        return sock

    def remove_client(self, sock):
        self._rlist.remove(sock)
        self._wlist.remove(sock)
        self.clients.remove(sock)
        return sock

    def run(self):
        while not self._stop:
            [rlist, wlist, xlist] = select.select(self._rlist, [], [])
            for fd in rlist:
                data = fd.recv(16384)

                if fd == self.control:
                    # FIXME move it to specific marshal
                    buf = io.BytesIO()
                    buf.write(data)
                    buf.seek(0)
                    cmd = cmdmsg(buf)
                    cmd.decode()
                    if cmd['command'] == IPRCMD_STOP:
                        self._stop = True
                        break
                    elif cmd['command'] == IPRCMD_RELOAD:
                        pass

                else:
                    if fd == self.netlink:
                        # a packet from local netlink
                        for sock in self.clients:
                            sock.send(data)
                        self.parse(data)
                    elif fd in self.clients:
                        # a packet from a client
                        if data == '':
                            # client socket is closed:
                            self.remove_client(fd)
                        else:
                            self.send(data)
                    elif fd in self.uplinks:
                        # a packet from an uplink
                        if data == '':
                            # uplink closed connection
                            self.remove_uplink(fd)
                        else:
                            self.parse(data)


class netlink(object):
    '''
    Main netlink messaging class. It automatically spawns threads
    to monitor network and netlink I/O, creates and destroys message
    queues.

    It can operate in three modes:
     * local system only
     * server mode
     * client mode

    By default, it starts in local system only mode. To start a
    server, you should call netlink.serve(url). The method
    can be called several times to listen on specific interfaces
    and/or ports.

    Alternatively, you can start the object in the client mode.
    In that case you should provide server url in the host
    parameter. You can not mix server and client modes, so
    message proxy/relay not possible yet. This will be fixed
    in the future.

    Urls should be specified in the form:
        (host, port)

    E.g.:
        nl = netlink(host=('127.0.0.1', 7000))
    '''

    family = NETLINK_GENERIC
    groups = 0
    marshal = marshal

    def __init__(self, debug=False, host='localsystem', interruptible=False):
        self.server = host
        self.iothread = iothread(self.marshal, self.send)
        self.listeners = self.iothread.listeners
        self.interruptible = interruptible
        if host == 'localsystem':
            self.socket = self.iothread.set_netlink(self.family, self.groups)
        else:
            self.socket = self.iothread.add_uplink(self.server)
        self.iothread.start()
        self.debug = debug
        self._nonce = 1
        self.servers = {}

    def shutdown(self, url=None):
        url = url or self.servers.keys()[0]
        self.servers[url].stop()
        del self.servers[url]

    def serve(self, url):
        self.servers[url] = server(url, self.iothread)
        self.servers[url].start()

    def nonce(self):
        '''
        Increment netlink protocol nonce (there is no need to
        call it directly)
        '''
        if self._nonce == 0xffffffff:
            self._nonce = 1
        else:
            self._nonce += 1
        return self._nonce

    def mirror(self, operate=True):
        '''
        Turn message mirroring on/off. When it is 'on', all
        received messages will be copied (mirrored) into the
        default 0 queue.
        '''
        self.monitor(operate)
        self.iothread.mirror = operate

    def monitor(self, operate=True):
        '''
        Create/destroy the default 0 queue. Netlink socket
        receives messages all the time, and there are many
        messages that are not replies. They are just
        generated by the kernel as a reflection of settings
        changes. To start receiving these messages, call
        netlink.monitor(). They can be fetched by
        netlink.get(0) or just netlink.get().
        '''
        if operate:
            self.listeners[0] = Queue.Queue()
        else:
            del self.listeners[0]

    def get(self, key=0, interruptible=False):
        '''
        Get a message from a queue

        * key -- message queue number
        * interruptible -- catch ctrl-c

        Please note, that setting interruptible=True will cause
        polling overhead. Python starts implied poll cycle, if
        timeout is set.
        '''
        queue = self.listeners[key]
        interruptible = interruptible or self.interruptible
        if interruptible:
            tot = 31536000
        else:
            tot = None
        result = []
        while True:
            # timeout is set to catch ctrl-c
            # Bug-Url: http://bugs.python.org/issue1360
            msg = queue.get(block=True, timeout=tot)
            if msg['header']['error'] is not None:
                raise msg['header']['error']
            if msg['header']['type'] != NLMSG_DONE:
                result.append(msg)
            if (msg['header']['type'] == NLMSG_DONE) or \
               (not msg['header']['flags'] & NLM_F_MULTI):
                break
        # not default queue
        if key != 0:
            # delete the queue
            del self.listeners[key]
            # get remaining messages from the queue and
            # re-route them to queue 0 or drop
            while not queue.empty():
                msg = queue.get(bloc=True, timeout=tot)
                if 0 in self.listeners:
                    self.listeners[0].put(msg)
        return result

    def send(self, buf):
        '''
        Send a buffer or to the local kernel, or to
        the server, depending on the setup.
        '''
        if self.server == 'localsystem':
            self.socket.sendto(buf, (0, 0))
        else:
            self.socket.send(buf)

    def nlm_request(self, msg, msg_type,
                    msg_flags=NLM_F_DUMP | NLM_F_REQUEST):
        '''
        Send netlink request, filling common message
        fields, and wait for response.
        '''
        # FIXME make it thread safe, yeah
        nonce = self.nonce()
        self.listeners[nonce] = Queue.Queue()
        msg['header']['sequence_number'] = nonce
        msg['header']['pid'] = os.getpid()
        msg['header']['type'] = msg_type
        msg['header']['flags'] = msg_flags
        msg.encode()
        self.send(msg.buf.getvalue())
        result = self.get(nonce)
        if not self.debug:
            for i in result:
                del i['header']
        return result
