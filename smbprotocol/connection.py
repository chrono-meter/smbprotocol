import copy
import hashlib
import hmac
import logging
import os
from datetime import datetime
from multiprocessing.dummy import Lock
from queue import Empty

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.ciphers import aead, algorithms

from smbprotocol.constants import Capabilities, Ciphers, Commands, Dialects, \
    HashAlgorithms, NegotiateContextType, NtStatus, SecurityMode, Smb1Flags2, \
    Smb2Flags
from smbprotocol.exceptions import SMBResponseException
from smbprotocol.messages import SMB2PacketHeader, SMB3PacketHeader, \
    SMB3NegotiateRequest, \
    SMB2PreauthIntegrityCapabilities, SMB2NegotiateContextRequest, \
    SMB1PacketHeader, SMB1NegotiateRequest, SMB2NegotiateResponse, \
    SMB2EncryptionCapabilities, SMB2NegotiateRequest, \
    SMB2TransformHeader
from smbprotocol.transport import Tcp

log = logging.getLogger(__name__)


class Connection(object):

    def __init__(self, guid, server_name, port, require_signing=True):
        """
        [MS-SMB2] v53.0 2017-09-15

        3.2.1.2 Per SMB2 Transport Connection
        Used as the transport interface for a server. Some values have been
        omitted as they can be retrieved by the Server object stored in
        self.server

        :param guid: The client guid generated in Client
        :param server_name: The server to start the connection
        :param port: The port to use for the transport
        :param require_signing: Whether signing is required on SMB messages
        """
        log.info("Initialising connection, guid: %s, require_singing: %s, "
                 "server_name: %s, port: %d"
                 % (guid, require_signing, server_name, port))
        self.server_name = server_name
        self.port = port
        self.transport = Tcp(server_name, port)

        # Table of Session entries
        self.session_table = {}

        # Table of sessions that have not completed authentication, indexed by
        # session_id
        self.preauth_session_table = {}

        # Table of Requests that have yet to be picked up by the application,
        # it MAY contain a response from the server as well
        self.outstanding_requests = dict()

        # Table of available sequence numbers
        self.sequence_window = dict(
            low=0,
            high=0
        )

        # Byte array containing the negotiate token and remembered for
        # authentication
        self.gss_negotiate_token = None

        self.server_guid = None
        self.max_transact_size = None
        self.max_read_size = None
        self.max_write_size = None
        self.require_signing = None

        # SMB 2.1+
        self.dialect = None
        self.supports_file_leasing = None
        self.supports_multi_credit = None
        self.client_guid = guid

        # SMB 3.x+
        self.supports_directory_leasing = None
        self.supports_multi_channel = None
        self.supports_persistent_handles = None
        self.supports_encryption = None

        # TODO: Add more capabilities
        self.client_capabilities = Capabilities.SMB2_GLOBAL_CAP_ENCRYPTION
        self.client_security_mode = \
            SecurityMode.SMB2_NEGOTIATE_SIGNING_REQUIRED if \
            require_signing else SecurityMode.SMB2_NEGOTIATE_SIGNING_ENABLED
        self.server_security_mode = None
        self.server_capabilities = None

        # SMB 3.1.1+
        # The hashing algorithm object that was negotiated
        self.preauth_integrity_hash_id = None

        # Preauth integrity hash value computed for the SMB2 NEGOTIATE request
        # contains the messages used to compute the hash
        self.preauth_integrity_hash_value = []

        # The cipher object that was negotiated
        self.cipher_id = None

        # used to ensure sequence num/message id's are gathered/sent in the
        # same order if running in multiple threads
        self.lock = Lock()

    def connect(self, dialect=None):
        """
        [MS-SMB2] v53.0 2017-09-15

        3.2.4.2.1 Connecting to the Target Server
        Will connect to the target server using the connection specified. Once
        connected will negotiate that capabilities with the SMB service, it
        does this by sending an SMB1 negotiate message then finally an SMB2
        negotiate message.
        """
        log.info("Setting up transport connection")
        self.transport.connect()

        log.info("Starting negotiation with SMB server")
        smb_response = self._send_smb1_negotiate(dialect)

        # Renegotiate with SMB2NegotiateRequest if 2.??? was received back
        if smb_response['dialect_revision'].get_value() == \
                Dialects.SMB_2_WILDCARD:
            smb_response = self._send_smb2_negotiate()

        log.info("Negotiated dialect: %s"
                 % str(smb_response['dialect_revision']))
        self.dialect = smb_response['dialect_revision'].get_value()
        self.max_transact_size = smb_response['max_transact_size'].get_value()
        self.max_read_size = smb_response['max_read_size'].get_value()
        self.max_write_size = smb_response['max_write_size'].get_value()
        self.server_guid = smb_response['server_guid'].get_value()
        self.gss_negotiate_token = smb_response['buffer'].get_value()

        self.require_signing = smb_response['security_mode'].get_value() == \
            SecurityMode.SMB2_NEGOTIATE_SIGNING_REQUIRED
        log.info("Connection require signing: %s" % self.require_signing)
        capabilities = smb_response['capabilities']

        # SMB 2.1
        if self.dialect >= Dialects.SMB_2_1_0:
            self.supports_file_leasing = \
                capabilities.has_flag(Capabilities.SMB2_GLOBAL_CAP_LEASING)
            self.supports_multi_credit = \
                capabilities.has_flag(Capabilities.SMB2_GLOBAL_CAP_MTU)

        # SMB 3.x
        if self.dialect >= Dialects.SMB_3_0_0:
            self.supports_directory_leasing = capabilities.has_flag(
                Capabilities.SMB2_GLOBAL_CAP_DIRECTORY_LEASING)
            self.supports_multi_channel = capabilities.has_flag(
                Capabilities.SMB2_GLOBAL_CAP_MULTI_CHANNEL)

            # TODO: SMB2_GLOBAL_CAP_PERSISTENT_HANDLES
            self.supports_persistent_handles = False
            self.supports_encryption = capabilities.has_flag(
                Capabilities.SMB2_GLOBAL_CAP_ENCRYPTION) \
                and self.dialect < Dialects.SMB_3_1_1
            self.server_capabilities = capabilities
            self.server_security_mode = \
                smb_response['security_mode'].get_value()

            # TODO: Check/add server to server_list in Client Page 203

        # SMB 3.1
        if self.dialect >= Dialects.SMB_3_1_1:
            for context in smb_response['negotiate_context_list']:
                if context['context_type'].get_value() == \
                        NegotiateContextType.SMB2_ENCRYPTION_CAPABILITIES:
                    cipher_id = context['data']['ciphers'][0]
                    self.cipher_id = Ciphers.get_cipher(cipher_id)
                    self.supports_encryption = self.cipher_id != 0
                else:
                    hash_id = context['data']['hash_algorithms'][0]
                    self.preauth_integrity_hash_id = \
                        HashAlgorithms.get_algorithm(hash_id)

    def disconnect(self):
        log.info("Disconnecting transport connection")
        self.transport.disconnect()

    def send(self, message, command, session=None, tree=None):
        """
        Sends a message
        :return:
        """
        if command == Commands.SMB2_NEGOTIATE:
            header = SMB2PacketHeader()
        elif self.dialect < Dialects.SMB_3_0_0:
            header = SMB2PacketHeader()
        else:
            header = SMB3PacketHeader()

        header['command'] = command
        header['flags'].set_flag(Smb2Flags.SMB2_FLAGS_PRIORITY_MASK)

        if session:
            header['session_id'] = session.session_id
        if tree:
            header['tree_id'] = tree.tree_connect_id

        # when run in a thread or subprocess, getting the message id and
        # sending the messages in order are important
        self.lock.acquire()
        # TODO: pass through the message id to cancel
        message_id = 0
        if command != Commands.SMB2_CANCEL:
            message_id = self.sequence_window['low']
            self._increment_sequence_windows(1)

        header['message_id'] = message_id
        log.info("Sending SMB Header for %s request" % str(header['command']))
        log.debug(str(header))

        # now add the actual data so we don't pollute the logs too much
        header['data'] = message

        if session and session.encrypt_data and session.encryption_key:
            header = self._encrypt(header, session)
        elif session and session.signing_required and session.signing_key:
            self._sign(header, session)

        request = Request(header)
        self.outstanding_requests[message_id] = request
        self.transport.send(request)
        self.lock.release()

        return header

    def receive(self, message_id):
        """
        # 3.2.5.1 - Receiving Any Message
        :return:
        """
        request = self.outstanding_requests.get(message_id, None)
        if not request:
            raise Exception("No request with the ID %d is expecting a response"
                            % message_id)

        # check if we have received a response
        response = None
        if request.response:
            response = request.response
        else:
            # otherwise wait until we receive a response
            while not response:
                self._flush_message_buffer()
                request = self.outstanding_requests[message_id]
                response = request.response

        status = response['status'].get_value()

        if status == NtStatus.STATUS_PENDING:
            request.response = None
            self.outstanding_requests[message_id] = request

        if status != NtStatus.STATUS_SUCCESS:
            raise SMBResponseException(response, status, message_id)

        # now we have a retrieval request for the response, we can delete the
        # request from the outstanding requests
        del self.outstanding_requests[message_id]

        return response

    def _flush_message_buffer(self):
        """
        Loops through the transport message_buffer until there are no messages
        left in the queue. Each response is assigned to the Request object
        based on the message_id which are then available in
        self.outstanding_requests
        :return: None
        """
        while True:
            try:
                message = self.transport.message_buffer.get(block=False)
            except Empty:
                # raises Empty if wait=False and there are no messages, in this
                # case we have nothing to parse and so break from the loop
                break

            # if bytes then it is an unknown message, if TransformHeader we
            # need to decrypt it
            if isinstance(message, bytes):
                raise Exception("Invalid header '%s' received from server"
                                % message[:4])
            elif isinstance(message, SMB2TransformHeader):
                message = self._decrypt(message)
            self._verify(message)

            message_id = message['message_id'].get_value()
            request = self.outstanding_requests.get(message_id, None)
            if not request:
                raise Exception("Received request with an unknown message ID: "
                                "%d" % message_id)
            request.response = message
            self.outstanding_requests[message_id] = request

    def _sign(self, message, session):
        message['flags'].set_flag(Smb2Flags.SMB2_FLAGS_SIGNED)
        signature = self._generate_signature(message, session)
        message['signature'] = signature

    def _verify(self, message, verify_session=False):
        if message['message_id'].get_value() == 0xFFFFFFFFFFFFFFFF:
            return
        elif not message['flags'].has_flag(Smb2Flags.SMB2_FLAGS_SIGNED):
            return
        elif message['command'].get_value() == Commands.SMB2_SESSION_SETUP \
                and not verify_session:
            return

        session_id = message['session_id'].get_value()
        session = self.session_table.get(session_id, None)
        if session is None:
            raise Exception("Failed to find session %d for message "
                            "verification" % session_id)
        expected = self._generate_signature(message, session)
        actual = message['signature'].get_value()
        if actual != expected:
            raise Exception("Server message signature could not be verified: "
                            "%s != %s" % (actual, expected))

    def _generate_signature(self, message, session):
        msg = copy.deepcopy(message)
        msg['signature'] = b"\x00" * 16
        msg_data = msg.pack()

        if self.dialect >= Dialects.SMB_3_0_0:
            # TODO: work out when to get channel.signing_key
            signing_key = session.signing_key

            c = cmac.CMAC(algorithms.AES(signing_key),
                          backend=default_backend())
            c.update(msg_data)
            signature = c.finalize()
        else:
            signing_key = session.signing_key
            hmac_algo = hmac.new(signing_key, msg=msg_data,
                                 digestmod=hashlib.sha256)
            signature = hmac_algo.digest()[:16]

        return signature

    def _encrypt(self, message, session):
        """
        [MS-SMB2] v53.0 2017-09-15

        3.1.4.3 Encrypting the Message
        Encrypts the message usinig the encryption keys negotiated with.

        :param message: The message to encrypt
        :param session: The session associated with the message
        :return: The encrypted message in a SMB2 TRANSFORM_HEADER
        """

        header = SMB2TransformHeader()
        header['original_message_size'] = len(message)
        header['session_id'] = message['session_id'].get_value()

        encryption_key = session.encryption_key
        if self.dialect >= Dialects.SMB_3_1_1:
            cipher = self.cipher_id
        else:
            cipher = Ciphers.get_cipher(Ciphers.AES_128_CCM)
        if cipher == aead.AESGCM:
            nonce = os.urandom(12)
            header['nonce'] = nonce + (b"\x00" * 4)
        else:
            nonce = os.urandom(11)
            header['nonce'] = nonce + (b"\x00" * 5)

        cipher_text = cipher(encryption_key).encrypt(nonce, message.pack(),
                                                     header.pack()[20:])
        signature = cipher_text[-16:]
        enc_message = cipher_text[:-16]

        header['signature'] = signature
        header['data'] = enc_message

        return header

    def _decrypt(self, message):
        """
        [MS-SMB2] v53.0 2017-09-15

        3.2.5.1.1 Decrypting the Message
        This will decrypt the message and convert the raw bytes value returned
        by direct_tcp to a SMB Header structure

        :param message: The message to decrypt
        :return: The decrypted message including the header
        """
        if message['flags'].get_value() != 0x0001:
            raise Exception("Expecting flag of 0x0001 in SMB Transform Header "
                            "Response")

        session_id = message['session_id'].get_value()
        session = self.session_table.get(session_id, None)
        if session is None:
            raise Exception("Failed to find session %s for message decryption"
                            % session_id)

        if self.dialect >= Dialects.SMB_3_1_1:
            cipher = self.cipher_id
        else:
            cipher = Ciphers.get_cipher(Ciphers.AES_128_CCM)

        if cipher == aead.AESGCM:
            nonce = message['nonce'].get_value()[:12]
        else:
            nonce = message['nonce'].get_value()[:11]

        signature = message['signature'].get_value()
        enc_message = message['data'].get_value() + signature

        c = cipher(session.decryption_key)
        dec_message = c.decrypt(nonce, enc_message, message.pack()[20:52])

        packet = SMB2PacketHeader()
        packet.unpack(dec_message)

        return packet

    def _send_smb1_negotiate(self, dialect):
        header = SMB1PacketHeader()
        header['command'] = 0x72  # SMBv1 Negotiate Protocol
        header['flags2'] = Smb1Flags2.SMB_FLAGS2_LONG_NAME | \
            Smb1Flags2.SMB_FLAGS2_EXTENDED_SECURITY | \
            Smb1Flags2.SMB_FLAGS2_NT_STATUS | \
            Smb1Flags2.SMB_FLAGS2_UNICODE
        header['data'] = SMB1NegotiateRequest()
        dialects = b"\x02SMB 2.002\x00"
        if dialect != Dialects.SMB_2_0_2:
            dialects += b"\x02SMB 2.???\x00"
        header['data']['dialects'] = dialects
        request = Request(header)

        log.info("Sending SMB1 Negotiate message with dialects: %s" % dialects)
        log.debug(str(header))
        self.transport.send(request)

        self._increment_sequence_windows(1)
        response = self.transport.message_buffer.get(block=True)
        log.info("Receiving SMB1 Negotiate response")
        log.debug(str(response))
        smb_response = SMB2NegotiateResponse()
        try:
            smb_response.unpack(response['data'].get_value())
        except Exception as exc:
            raise Exception("Expecting SMB2NegotiateResponse message type in "
                            "response but could not unpack data: %s"
                            % str(exc))

        return smb_response

    def _send_smb2_negotiate(self):
        self.salt = os.urandom(32)

        if self.dialect is None:
            neg_req = SMB3NegotiateRequest()
            self.negotiated_dialects = [
                Dialects.SMB_2_0_2,
                Dialects.SMB_2_1_0,
                Dialects.SMB_3_0_0,
                Dialects.SMB_3_0_2,
                Dialects.SMB_3_1_1
            ]
            highest_dialect = Dialects.SMB_3_1_1
        else:
            if self.dialect >= Dialects.SMB_3_1_1:
                neg_req = SMB3NegotiateRequest()
            else:
                neg_req = SMB2NegotiateRequest()
            self.negotiated_dialects = [
                self.dialect
            ]
            highest_dialect = self.dialect
        neg_req['dialects'] = self.negotiated_dialects
        log.info("Negotiating with SMB2 protocol with highest client dialect "
                 "of: %s" % [dialect for dialect, v in vars(Dialects).items()
                             if v == highest_dialect][0])

        neg_req['security_mode'] = self.client_security_mode

        if highest_dialect >= Dialects.SMB_2_1_0:
            log.debug("Adding client guid %s to negotiate request"
                      % self.client_guid)
            neg_req['client_guid'] = self.client_guid

        if highest_dialect >= Dialects.SMB_3_0_0:
            log.debug("Adding client capabilities %d to negotiate request"
                      % self.client_capabilities)
            neg_req['capabilities'] = self.client_capabilities

        if highest_dialect >= Dialects.SMB_3_1_1:
            int_cap = SMB2NegotiateContextRequest()
            int_cap['context_type'] = \
                NegotiateContextType.SMB2_PREAUTH_INTEGRITY_CAPABILITIES
            int_cap['data'] = SMB2PreauthIntegrityCapabilities()
            int_cap['data']['hash_algorithms'] = [
                HashAlgorithms.SHA_512
            ]
            int_cap['data']['salt'] = self.salt
            log.debug("Adding preauth integrity capabilities of hash SHA512 "
                      "and salt %s to negotiate request" % self.salt)

            enc_cap = SMB2NegotiateContextRequest()
            enc_cap['context_type'] = \
                NegotiateContextType.SMB2_ENCRYPTION_CAPABILITIES
            enc_cap['data'] = SMB2EncryptionCapabilities()
            enc_cap['data']['ciphers'] = [
                Ciphers.AES_128_GCM,
                Ciphers.AES_128_CCM
            ]
            # remove extra padding for last list entry
            enc_cap['padding'].size = 0
            enc_cap['padding'] = b""
            log.debug("Adding encryption capabilities of AES128 GCM and "
                      "AES128 CCM to negotiate request")

            neg_req['negotiate_context_list'] = [
                int_cap,
                enc_cap
            ]

        log.info("Sending SMB2 Negotiate message")
        log.debug(str(neg_req))
        header = self.send(neg_req, Commands.SMB2_NEGOTIATE)
        self.preauth_integrity_hash_value.append(header)

        response = self.receive(header['message_id'].get_value())
        log.info("Receiving SMB2 Negotiate response")
        log.debug(str(response))
        self.preauth_integrity_hash_value.append(response)

        smb_response = SMB2NegotiateResponse()
        smb_response.unpack(response['data'].get_value())

        return smb_response

    def _increment_sequence_windows(self, credit_charge):
        high_value = self.sequence_window['high']
        self.sequence_window['low'] = high_value + credit_charge
        self.sequence_window['high'] = high_value + credit_charge


class Request(object):

    def __init__(self, message):
        """
        [MS-SMB2] v53.0 2017-09-15

        3.2.1.7 Per Pending Request
        For each request that was sent to the server and is await a response
        :param message: The message to be sent in the request
        """
        self.cancel_id = os.urandom(8)
        self.async_id = os.urandom(8)
        self.message = message
        self.timestamp = datetime.now()

        # not in SMB spec
        # Used to contain the corresponding response from the server as the
        # receiving in done in parallel
        self.response = None
