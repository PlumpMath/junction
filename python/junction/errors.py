class MessageCutOff(Exception):
    "A peer connection terminated mid-message"

class _BailOutOfListener(Exception):
    pass

class BadHandshake(Exception):
    "Unexpected message while trying to establish a peering"

class Unroutable(Exception):
    "A message does not have any peers's registrations"

class NoRemoteHandler(Exception):
    "An RPC was mistakenly sent to a peer"

class RemoteException(Exception):
    "An unexpected exception occurred in the peer handling an RPC"

class LostConnection(Exception):
    "The connection closed while waiting for a response"

class UnrecognizedRemoteProblem(Exception):
    "Improperly formatted RPC error response"

class DependentCallbackException(Exception):
    "An exception in a Dependent's callback function"

class WaitTimeout(Exception):
    "Exception raised when a wait outlasts a specified timeout"

class AlreadyComplete(Exception):
    "Exception raised on abort() of an already-completed future"


HANDLED_ERROR_TYPES = {}

class _MetaHandledError(type):
    def __init__(cls, *args, **kwargs):
        if cls.code in HANDLED_ERROR_TYPES:
            raise Exception("HandledError subclasses need unique codes")

        HANDLED_ERROR_TYPES[cls.code] = cls

class HandledError(Exception):
    __metaclass__ = _MetaHandledError
    code = 0