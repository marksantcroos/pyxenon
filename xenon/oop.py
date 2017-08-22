from .proto import xenon_pb2

try:
    from enum import Enum
except ImportError as e:
    use_enum = False

    class Enum(object):
        """Minimal Enum replacement."""
        def __init__(self, name, items):
            for k, v in items:
                setattr(self, k, v)

else:
    use_enum = True

try:
    from inspect import (Signature, Parameter, signature)
except ImportError as e:
    use_signature = False
else:
    use_signature = True


def mirror_enum(name):
    grpc_enum = getattr(xenon_pb2, name)
    return Enum(name, grpc_enum.items())


def to_camel_case(name):
    return ''.join(w.title() for w in name.split('_'))


def to_lower_camel_case(name):
    words = name.split('_')
    return words[0] + ''.join(w.title() for w in words[1:])


def get_fields(msg_type):
    """Get a list of field names for Grpc message."""
    return list(f.name for f in msg_type.DESCRIPTOR.fields)


class GrpcMethod:
    """Data container for a GRPC method.

    :ivar name: underscore style of method name
    :ivar uses_request: wether this method has a request, if this value is
        `True`, the name is generated from `name`, if it is a string the
        contents of this string are used.
    :ivar field_name: name of `self` within the request.
    :ivar input_transform: custom method to generate a request from the
        method's arguments.
    :ivar output_transform: custom method to extract the return value from
        the return value.
    """
    def __init__(self, name, uses_request=False, field_name=None,
                 input_transform=None, output_transform=None):
        self.name = name
        self.uses_request = uses_request
        self.field_name = field_name
        self.input_transform = input_transform
        self.output_transform = output_transform

    @property
    def is_simple(self):
        return not self.uses_request and not self.input_transform

    @property
    def request_name(self):
        """Generate the name of the request."""
        if not self.uses_request:
            return None

        if isinstance(self.uses_request, str):
            return self.uses_request

        return to_camel_case(self.name) + "Request"

    @property
    def request_type(self):
        """Retrieve the type of the request, by fetching it from
        `xenon.proto.xenon_pb2`."""
        if not self.uses_request:
            return None

        return getattr(xenon_pb2, self.request_name)

    # python 3 only
    @property
    def signature(self):
        """Create a signature for this method, only in Python > 3.4"""
        if not use_signature:
            raise NotImplementedError("Python 3 only.")

        parameters = \
            (Parameter(name='self', kind=Parameter.POSITIONAL_ONLY),)

        if self.input_transform:
            return signature(self.input_transform)

        if self.uses_request:
            fields = get_fields(self.request_type)
            assert self.field_name in fields
            fields.remove(self.field_name)
            parameters += tuple(
                Parameter(name=name, kind=Parameter.POSITIONAL_OR_KEYWORD,
                          default=None)
                for name in fields)

        return Signature(parameters)

    # TODO extend documentation rendered from proto
    def docstring(self, servicer):
        """Generate a doc-string."""
        s = getattr(servicer, to_lower_camel_case(self.name)).__doc__ or ""

        if self.uses_request:
            s += "\n"
            for field in get_fields(self.request_type):
                if field != self.field_name:
                    s += "    :param {}: {}\n".format(field, field)

        return s


def unwrap(arg):
    if hasattr(arg, '__is_proxy__'):
        return arg.__wrapped__
    else:
        return arg


def make_request(self, method, *args, **kwargs):
    """Creates a request from a method function call."""
    if args and not use_signature:
        raise NotImplementedError("Only keyword arguments allowed in Python2")

    new_kwargs = {kw: unwrap(value) for kw, value in kwargs.items()}

    if use_signature:
        new_args = tuple(unwrap(value) for value in args)
        bound_args = method.signature.bind(
                unwrap(self), *new_args, **new_kwargs).arguments

        # if we encounter any Enum arguments, replace them with their value
        for k in bound_args:
            if isinstance(bound_args[k], Enum):
                bound_args[k] = bound_args[k].value

        # replace `self` with the correct keyword
        new_kwargs = {(kw if kw != 'self' else method.field_name): v
                      for kw, v in bound_args.items()}

        args = tuple(x.value if isinstance(x, Enum) else x for x in args)

    else:
        new_kwargs[self.field_name] = unwrap(self)

    return getattr(xenon_pb2, method.request_name)(**new_kwargs)


def apply_transform(self, t, x):
    """Apply a transformation using `self` as object reference."""
    if t is None:
        return x
    else:
        return t(self.__service__, x)


def transform_map(f):
    def t(self, xs):
        return (f(self, x) for x in xs)

    return t


def method_wrapper(m):
    """Generates a method from a `GrpcMethod` definition."""

    def simple_method(self):
        f = getattr(self.__service__, to_lower_camel_case(m.name))
        return apply_transform(self, m.output_transform, f(unwrap(self)))

    if m.is_simple:
        return simple_method

    def transform_method(self, *args, **kwargs):
        f = getattr(self.__service__, to_lower_camel_case(m.name))
        request = m.input_transform(self, *args, **kwargs)
        return apply_transform(self, m.output_transform, f(request))

    if m.input_transform is not None:
        return transform_method

    def request_method(self, *args, **kwargs):
        f = getattr(self.__service__, to_lower_camel_case(m.name))
        request = make_request(self, m, *args, **kwargs)
        return apply_transform(self, m.output_transform, f(request))

    return request_method


class OopMeta(type):
    """Meta class for Grpc Object wrappers."""
    def __new__(cls, name, parents, dct):
        return super(OopMeta, cls).__new__(cls, name, parents, dct)

    def __init__(cls, name, parents, dct):
        super(OopMeta, cls).__init__(name, parents, dct)

        for m in cls.__methods__():
            f = method_wrapper(m)
            if use_signature:
                f.__signature__ = m.signature

            if cls.__servicer__:
                f.__doc__ = m.docstring(cls.__servicer__)

            setattr(cls, m.name, f)


class OopProxy(metaclass=OopMeta):
    """Base class for Grpc Object wrappers. Ensures basic object sanity,
    namely the existence of `__service__` and `__wrapped__` members and
    the using of `OopMeta` meta-class. Also manages retrieving attributes
    from the wrapped instance.

    :ivar __is_proxy__: if True, this value represents a wrapped value,
        from which the GRPC message can be extraced by getting the
        `__wrapped__` attribute.
    :ivar __servicer__: if applicable, this gives the GRPC servicer class
        associated with the proxy object; this is used to retrieve doc-strings.
    """

    __is_proxy__ = True
    __servicer__ = None

    @classmethod
    def __methods__(cls):
        """This method should return a list of GRPCMethod objects."""
        return []

    def __init__(self, service, wrapped):
        self.__service__ = service
        self.__wrapped__ = wrapped

    def __getattr__(self, attr):
        """Accesses fields of the corresponding GRPC message."""
        return getattr(self.__wrapped__, attr)
