#! /usr/bin/python
#
# pgdb.py
#
# Written by D'Arcy J.M. Cain
#
# $Id$
#

"""pgdb - DB-API 2.0 compliant module for PygreSQL.

(c) 1999, Pascal Andre <andre@via.ecp.fr>.
See package documentation for further information on copyright.

Inline documentation is sparse.
See DB-API 2.0 specification for usage information:
http://www.python.org/peps/pep-0249.html

Basic usage:

    pgdb.connect(connect_string) # open a connection
    # connect_string = 'host:database:user:password:opt'
    # All parts are optional. You may also pass host through
    # password as keyword arguments. To pass a port,
    # pass it in the host keyword parameter:
    connection = pgdb.connect(host='localhost:5432')

    cursor = connection.cursor() # open a cursor

    cursor.execute(query[, params])
    # Execute a query, binding params (a dictionary) if they are
    # passed. The binding syntax is the same as the % operator
    # for dictionaries, and no quoting is done.

    cursor.executemany(query, list of params)
    # Execute a query many times, binding each param dictionary
    # from the list.

    cursor.fetchone() # fetch one row, [value, value, ...]

    cursor.fetchall() # fetch all rows, [[value, value, ...], ...]

    cursor.fetchmany([size])
    # returns size or cursor.arraysize number of rows,
    # [[value, value, ...], ...] from result set.
    # Default cursor.arraysize is 1.

    cursor.description # returns information about the columns
    #	[(column_name, type_name, display_size,
    #		internal_size, precision, scale, null_ok), ...]
    # Note that display_size, precision, scale and null_ok
    # are not implemented.

    cursor.rowcount # number of rows available in the result set
    # Available after a call to execute.

    connection.commit() # commit transaction

    connection.rollback() # or rollback transaction

    cursor.close() # close the cursor

    connection.close() # close the connection
"""

from __future__ import print_function

from _pg import *

from datetime import date, time, datetime, timedelta
from time import localtime
from decimal import Decimal
from math import isnan, isinf
from collections import namedtuple
from json import loads as jsondecode, dumps as jsonencode

try:
    long
except NameError:  # Python >= 3.0
    long = int

try:
    unicode
except NameError:  # Python >= 3.0
    unicode = str

try:
    basestring
except NameError:  # Python >= 3.0
    basestring = (str, bytes)

from collections import Iterable
try:
    from collections import OrderedDict
except ImportError:  # Python 2.6 or 3.0
    try:
        from ordereddict import OrderedDict
    except Exception:
        def OrderedDict(*args):
            raise NotSupportedError('OrderedDict is not supported')


### Module Constants

# compliant with DB API 2.0
apilevel = '2.0'

# module may be shared, but not connections
threadsafety = 1

# this module use extended python format codes
paramstyle = 'pyformat'

# shortcut methods have been excluded from DB API 2 and
# are not recommended by the DB SIG, but they can be handy
shortcutmethods = 1


### Internal Type Handling

def decimal_type(decimal_type=None):
    """Get or set global type to be used for decimal values.

    Note that connections cache cast functions. To be sure a global change
    is picked up by a running connection, call con.type_cache.reset_typecast().
    """
    global Decimal
    if decimal_type is not None:
        Decimal = decimal_type
        set_typecast('numeric', decimal_type)
    return Decimal


def cast_bool(value):
    """Cast boolean value in database format to bool."""
    if value:
        return value[0] in ('t', 'T')


def cast_money(value):
    """Cast money value in database format to Decimal."""
    if value:
        value = value.replace('(', '-')
        return Decimal(''.join(c for c in value if c.isdigit() or c in '.-'))


def cast_int2vector(value):
    """Cast an int2vector value."""
    return [int(v) for v in value.split()]


class Typecasts(dict):
    """Dictionary mapping database types to typecast functions.

    The cast functions get passed the string representation of a value in
    the database which they need to convert to a Python object.  The
    passed string will never be None since NULL values are already be
    handled before the cast function is called.
    """

    # the default cast functions
    # (str functions are ignored but have been added for faster access)
    defaults = {'char': str, 'bpchar': str, 'name': str,
        'text': str, 'varchar': str,
        'bool': cast_bool, 'bytea': unescape_bytea,
        'int2': int, 'int4': int, 'serial': int,
        'int8': long, 'json': jsondecode, 'jsonb': jsondecode,
        'oid': long, 'oid8': long,
        'float4': float, 'float8': float,
        'numeric': Decimal, 'money': cast_money,
        'int2vector': cast_int2vector,
        'anyarray': cast_array, 'record': cast_record}

    def __missing__(self, typ):
        """Create a cast function if it is not cached.

        Note that this class never raises a KeyError,
        but returns None when no special cast function exists.
        """
        if not isinstance(typ, str):
            raise TypeError('Invalid type: %s' % typ)
        cast = self.defaults.get(typ)
        if cast:
            # store default for faster access
            self[typ] = cast
        elif typ.startswith('_'):
            # create array cast
            base_cast = self[typ[1:]]
            cast = self.create_array_cast(base_cast)
            if base_cast:
                # store only if base type exists
                self[typ] = cast
        return cast

    def get(self, typ, default=None):
        """Get the typecast function for the given database type."""
        return self[typ] or default

    def set(self, typ, cast):
        """Set a typecast function for the specified database type(s)."""
        if isinstance(typ, basestring):
            typ = [typ]
        if cast is None:
            for t in typ:
                self.pop(t, None)
                self.pop('_%s' % t, None)
        else:
            if not callable(cast):
                raise TypeError("Cast parameter must be callable")
            for t in typ:
                self[t] = cast
                self.pop('_%s' % t, None)

    def reset(self, typ=None):
        """Reset the typecasts for the specified type(s) to their defaults.

        When no type is specified, all typecasts will be reset.
        """
        defaults = self.defaults
        if typ is None:
            self.clear()
            self.update(defaults)
        else:
            if isinstance(typ, basestring):
                typ = [typ]
            for t in typ:
                cast = defaults.get(t)
                if cast:
                    self[t] = cast
                    t = '_%s' % t
                    cast = defaults.get(t)
                    if cast:
                        self[t] = cast
                    else:
                        self.pop(t, None)
                else:
                    self.pop(t, None)
                    self.pop('_%s' % t, None)

    def create_array_cast(self, cast):
        """Create an array typecast for the given base cast."""
        return lambda v: cast_array(v, cast)

    def create_record_cast(self, name, fields, casts):
        """Create a named record typecast for the given fields and casts."""
        record = namedtuple(name, fields)
        return lambda v: record(*cast_record(v, casts))


_typecasts = Typecasts()  # this is the global typecast dictionary


def get_typecast(typ):
    """Get the global typecast function for the given database type(s)."""
    return _typecasts.get(typ)


def set_typecast(typ, cast):
    """Set a global typecast function for the given database type(s).

    Note that connections cache cast functions. To be sure a global change
    is picked up by a running connection, call con.type_cache.reset_typecast().
    """
    _typecasts.set(typ, cast)


def reset_typecast(typ=None):
    """Reset the global typecasts for the given type(s) to their default.

    When no type is specified, all typecasts will be reset.

    Note that connections cache cast functions. To be sure a global change
    is picked up by a running connection, call con.type_cache.reset_typecast().
    """
    _typecasts.reset(typ)


class LocalTypecasts(Typecasts):
    """Map typecasts, including local composite types, to cast functions."""

    defaults = _typecasts

    def __missing__(self, typ):
        """Create a cast function if it is not cached."""
        if typ.startswith('_'):
            base_cast = self[typ[1:]]
            cast = self.create_array_cast(base_cast)
            if base_cast:
                self[typ] = cast
        else:
            cast = self.defaults.get(typ)
            if cast:
                self[typ] = cast
            else:
                fields = self.get_fields(typ)
                if fields:
                    casts = [self[field.type] for field in fields]
                    fields = [field.name for field in fields]
                    cast = self.create_record_cast(typ, fields, casts)
                    self[typ] = cast
        return cast

    def get_fields(self, typ):
        """Return the fields for the given record type.

        This method will be replaced with a method that looks up the fields
        using the type cache of the connection.
        """
        return []


class TypeCode(str):
    """Class representing the type_code used by the DB-API 2.0.

    TypeCode objects are strings equal to the PostgreSQL type name,
    but carry some additional information.
    """

    @classmethod
    def create(cls, oid, name, len, type, category, delim, relid):
        """Create a type code for a PostgreSQL data type."""
        self = cls(name)
        self.oid = oid
        self.len = len
        self.type = type
        self.category = category
        self.delim = delim
        self.relid = relid
        return self

FieldInfo = namedtuple('FieldInfo', ['name', 'type'])


class TypeCache(dict):
    """Cache for database types.

    This cache maps type OIDs and names to TypeCode strings containing
    important information on the associated database type.
    """

    def __init__(self, cnx):
        """Initialize type cache for connection."""
        super(TypeCache, self).__init__()
        self._escape_string = cnx.escape_string
        self._src = cnx.source()
        self._typecasts = LocalTypecasts()
        self._typecasts.get_fields = self.get_fields

    def __missing__(self, key):
        """Get the type info from the database if it is not cached."""
        if isinstance(key, int):
            oid = key
        else:
            if '.' not in key and '"' not in key:
                key = '"%s"' % key
            oid = "'%s'::regtype" % self._escape_string(key)
        try:
            self._src.execute("SELECT oid, typname,"
                 " typlen, typtype, typcategory, typdelim, typrelid"
                " FROM pg_type WHERE oid=%s" % oid)
        except ProgrammingError:
            res = None
        else:
            res = self._src.fetch(1)
        if not res:
            raise KeyError('Type %s could not be found' % key)
        res = res[0]
        type_code = TypeCode.create(int(res[0]), res[1],
            int(res[2]), res[3], res[4], res[5], int(res[6]))
        self[type_code.oid] = self[str(type_code)] = type_code
        return type_code

    def get(self, key, default=None):
        """Get the type even if it is not cached."""
        try:
            return self[key]
        except KeyError:
            return default

    def get_fields(self, typ):
        """Get the names and types of the fields of composite types."""
        if not isinstance(typ, TypeCode):
            typ = self.get(typ)
            if not typ:
                return None
        if not typ.relid:
            return None  # this type is not composite
        self._src.execute("SELECT attname, atttypid"
            " FROM pg_attribute WHERE attrelid=%s AND attnum>0"
            " AND NOT attisdropped ORDER BY attnum" % typ.relid)
        return [FieldInfo(name, self.get(int(oid)))
            for name, oid in self._src.fetch(-1)]

    def get_typecast(self, typ):
        """Get the typecast function for the given database type."""
        return self._typecasts.get(typ)

    def set_typecast(self, typ, cast):
        """Set a typecast function for the specified database type(s)."""
        self._typecasts.set(typ, cast)

    def reset_typecast(self, typ=None):
        """Reset the typecast function for the specified database type(s)."""
        self._typecasts.reset(typ)

    def typecast(self, value, typ):
        """Cast the given value according to the given database type."""
        if value is None:
            # for NULL values, no typecast is necessary
            return None
        cast = self.get_typecast(typ)
        if not cast or cast is str:
            # no typecast is necessary
            return value
        return cast(value)


class _quotedict(dict):
    """Dictionary with auto quoting of its items.

    The quote attribute must be set to the desired quote function.
    """

    def __getitem__(self, key):
        return self.quote(super(_quotedict, self).__getitem__(key))


### Error messages

def _db_error(msg, cls=DatabaseError):
    """Return DatabaseError with empty sqlstate attribute."""
    error = cls(msg)
    error.sqlstate = None
    return error


def _op_error(msg):
    """Return OperationalError."""
    return _db_error(msg, OperationalError)


### Cursor Object

class Cursor(object):
    """Cursor object."""

    def __init__(self, dbcnx):
        """Create a cursor object for the database connection."""
        self.connection = self._dbcnx = dbcnx
        self._cnx = dbcnx._cnx
        self.type_cache = dbcnx.type_cache
        self._src = self._cnx.source()
        # the official attribute for describing the result columns
        self._description = None
        if self.row_factory is Cursor.row_factory:
            # the row factory needs to be determined dynamically
            self.row_factory = None
        else:
            self.build_row_factory = None
        self.rowcount = -1
        self.arraysize = 1
        self.lastrowid = None

    def __iter__(self):
        """Make cursor compatible to the iteration protocol."""
        return self

    def __enter__(self):
        """Enter the runtime context for the cursor object."""
        return self

    def __exit__(self, et, ev, tb):
        """Exit the runtime context for the cursor object."""
        self.close()

    def _quote(self, value):
        """Quote value depending on its type."""
        if value is None:
            return 'NULL'
        if isinstance(value, (datetime, date, time, timedelta, Json)):
            value = str(value)
        if isinstance(value, basestring):
            if isinstance(value, Binary):
                value = self._cnx.escape_bytea(value)
                if bytes is not str:  # Python >= 3.0
                    value = value.decode('ascii')
            else:
                value = self._cnx.escape_string(value)
            return "'%s'" % value
        if isinstance(value, float):
            if isinf(value):
                return "'-Infinity'" if value < 0 else "'Infinity'"
            if isnan(value):
                return "'NaN'"
            return value
        if isinstance(value, (int, long, Decimal, Literal)):
            return value
        if isinstance(value, list):
            # Quote value as an ARRAY constructor. This is better than using
            # an array literal because it carries the information that this is
            # an array and not a string.  One issue with this syntax is that
            # you need to add an explicit typecast when passing empty arrays.
            # The ARRAY keyword is actually only necessary at the top level.
            q = self._quote
            return 'ARRAY[%s]' % ','.join(str(q(v)) for v in value)
        if isinstance(value, tuple):
            # Quote as a ROW constructor.  This is better than using a record
            # literal because it carries the information that this is a record
            # and not a string.  We don't use the keyword ROW in order to make
            # this usable with the IN syntax as well.  It is only necessary
            # when the records has a single column which is not really useful.
            q = self._quote
            return '(%s)' % ','.join(str(q(v)) for v in value)
        try:
            value = value.__pg_repr__()
        except AttributeError:
            raise InterfaceError(
                'Do not know how to adapt type %s' % type(value))
        if isinstance(value, (tuple, list)):
            value = self._quote(value)
        return value

    def _quoteparams(self, string, parameters):
        """Quote parameters.

        This function works for both mappings and sequences.
        """
        if isinstance(parameters, dict):
            parameters = _quotedict(parameters)
            parameters.quote = self._quote
        else:
            parameters = tuple(map(self._quote, parameters))
        return string % parameters

    def _make_description(self, info):
        """Make the description tuple for the given field info."""
        name, typ, size, mod = info[1:]
        type_code = self.type_cache[typ]
        if mod > 0:
            mod -= 4
        if type_code == 'numeric':
            precision, scale = mod >> 16, mod & 0xffff
            size = precision
        else:
            if not size:
                size = type_info.size
            if size == -1:
                size = mod
            precision = scale = None
        return CursorDescription(name, type_code,
            None, size, precision, scale, None)

    @property
    def description(self):
        """Read-only attribute describing the result columns."""
        descr = self._description
        if self._description is True:
            make = self._make_description
            descr = [make(info) for info in self._src.listinfo()]
            self._description = descr
        return descr

    @property
    def colnames(self):
        """Unofficial convenience method for getting the column names."""
        return [d[0] for d in self.description]

    @property
    def coltypes(self):
        """Unofficial convenience method for getting the column types."""
        return [d[1] for d in self.description]

    def close(self):
        """Close the cursor object."""
        self._src.close()
        self._description = None
        self.rowcount = -1
        self.lastrowid = None

    def execute(self, operation, parameters=None):
        """Prepare and execute a database operation (query or command)."""
        # The parameters may also be specified as list of tuples to e.g.
        # insert multiple rows in a single operation, but this kind of
        # usage is deprecated.  We make several plausibility checks because
        # tuples can also be passed with the meaning of ROW constructors.
        if (parameters and isinstance(parameters, list)
                and len(parameters) > 1
                and all(isinstance(p, tuple) for p in parameters)
                and all(len(p) == len(parameters[0]) for p in parameters[1:])):
            return self.executemany(operation, parameters)
        else:
            # not a list of tuples
            return self.executemany(operation, [parameters])

    def executemany(self, operation, seq_of_parameters):
        """Prepare operation and execute it against a parameter sequence."""
        if not seq_of_parameters:
            # don't do anything without parameters
            return
        self._description = None
        self.rowcount = -1
        # first try to execute all queries
        rowcount = 0
        sql = "BEGIN"
        try:
            if not self._dbcnx._tnx:
                try:
                    self._cnx.source().execute(sql)
                except DatabaseError:
                    raise  # database provides error message
                except Exception as err:
                    raise _op_error("Can't start transaction")
                self._dbcnx._tnx = True
            for parameters in seq_of_parameters:
                sql = operation
                if parameters:
                    sql = self._quoteparams(sql, parameters)
                rows = self._src.execute(sql)
                if rows:  # true if not DML
                    rowcount += rows
                else:
                    self.rowcount = -1
        except DatabaseError:
            raise  # database provides error message
        except Error as err:
            raise _db_error(
                "Error in '%s': '%s' " % (sql, err), InterfaceError)
        except Exception as err:
            raise _op_error("Internal error in '%s': %s" % (sql, err))
        # then initialize result raw count and description
        if self._src.resulttype == RESULT_DQL:
            self._description = True  # fetch on demand
            self.rowcount = self._src.ntuples
            self.lastrowid = None
            if self.build_row_factory:
                self.row_factory = self.build_row_factory()
        else:
            self.rowcount = rowcount
            self.lastrowid = self._src.oidstatus()
        # return the cursor object, so you can write statements such as
        # "cursor.execute(...).fetchall()" or "for row in cursor.execute(...)"
        return self

    def fetchone(self):
        """Fetch the next row of a query result set."""
        res = self.fetchmany(1, False)
        try:
            return res[0]
        except IndexError:
            return None

    def fetchall(self):
        """Fetch all (remaining) rows of a query result."""
        return self.fetchmany(-1, False)

    def fetchmany(self, size=None, keep=False):
        """Fetch the next set of rows of a query result.

        The number of rows to fetch per call is specified by the
        size parameter. If it is not given, the cursor's arraysize
        determines the number of rows to be fetched. If you set
        the keep parameter to true, this is kept as new arraysize.
        """
        if size is None:
            size = self.arraysize
        if keep:
            self.arraysize = size
        try:
            result = self._src.fetch(size)
        except DatabaseError:
            raise
        except Error as err:
            raise _db_error(str(err))
        typecast = self.type_cache.typecast
        return [self.row_factory([typecast(value, typ)
            for typ, value in zip(self.coltypes, row)]) for row in result]

    def callproc(self, procname, parameters=None):
        """Call a stored database procedure with the given name.

        The sequence of parameters must contain one entry for each input
        argument that the procedure expects. The result of the call is the
        same as this input sequence; replacement of output and input/output
        parameters in the return value is currently not supported.

        The procedure may also provide a result set as output. These can be
        requested through the standard fetch methods of the cursor.
        """
        n = parameters and len(parameters) or 0
        query = 'select * from "%s"(%s)' % (procname, ','.join(n * ['%s']))
        self.execute(query, parameters)
        return parameters

    def copy_from(self, stream, table,
            format=None, sep=None, null=None, size=None, columns=None):
        """Copy data from an input stream to the specified table.

        The input stream can be a file-like object with a read() method or
        it can also be an iterable returning a row or multiple rows of input
        on each iteration.

        The format must be text, csv or binary. The sep option sets the
        column separator (delimiter) used in the non binary formats.
        The null option sets the textual representation of NULL in the input.

        The size option sets the size of the buffer used when reading data
        from file-like objects.

        The copy operation can be restricted to a subset of columns. If no
        columns are specified, all of them will be copied.
        """
        binary_format = format == 'binary'
        try:
            read = stream.read
        except AttributeError:
            if size:
                raise ValueError("Size must only be set for file-like objects")
            if binary_format:
                input_type = bytes
                type_name = 'byte strings'
            else:
                input_type = basestring
                type_name = 'strings'

            if isinstance(stream, basestring):
                if not isinstance(stream, input_type):
                    raise ValueError("The input must be %s" % type_name)
                if not binary_format:
                    if isinstance(stream, str):
                        if not stream.endswith('\n'):
                            stream += '\n'
                    else:
                        if not stream.endswith(b'\n'):
                            stream += b'\n'

                def chunks():
                    yield stream

            elif isinstance(stream, Iterable):

                def chunks():
                    for chunk in stream:
                        if not isinstance(chunk, input_type):
                            raise ValueError(
                                "Input stream must consist of %s" % type_name)
                        if isinstance(chunk, str):
                            if not chunk.endswith('\n'):
                                chunk += '\n'
                        else:
                            if not chunk.endswith(b'\n'):
                                chunk += b'\n'
                        yield chunk

            else:
                raise TypeError("Need an input stream to copy from")
        else:
            if size is None:
                size = 8192
            elif not isinstance(size, int):
                raise TypeError("The size option must be an integer")
            if size > 0:

                def chunks():
                    while True:
                        buffer = read(size)
                        yield buffer
                        if not buffer or len(buffer) < size:
                            break

            else:

                def chunks():
                    yield read()

        if not table or not isinstance(table, basestring):
            raise TypeError("Need a table to copy to")
        if table.lower().startswith('select'):
                raise ValueError("Must specify a table, not a query")
        else:
            table = '"%s"' % (table,)
        operation = ['copy %s' % (table,)]
        options = []
        params = []
        if format is not None:
            if not isinstance(format, basestring):
                raise TypeError("The frmat option must be be a string")
            if format not in ('text', 'csv', 'binary'):
                raise ValueError("Invalid format")
            options.append('format %s' % (format,))
        if sep is not None:
            if not isinstance(sep, basestring):
                raise TypeError("The sep option must be a string")
            if format == 'binary':
                raise ValueError(
                    "The sep option is not allowed with binary format")
            if len(sep) != 1:
                raise ValueError(
                    "The sep option must be a single one-byte character")
            options.append('delimiter %s')
            params.append(sep)
        if null is not None:
            if not isinstance(null, basestring):
                raise TypeError("The null option must be a string")
            options.append('null %s')
            params.append(null)
        if columns:
            if not isinstance(columns, basestring):
                columns = ','.join('"%s"' % (col,) for col in columns)
            operation.append('(%s)' % (columns,))
        operation.append("from stdin")
        if options:
            operation.append('(%s)' % ','.join(options))
        operation = ' '.join(operation)

        putdata = self._src.putdata
        self.execute(operation, params)

        try:
            for chunk in chunks():
                putdata(chunk)
        except BaseException as error:
            self.rowcount = -1
            # the following call will re-raise the error
            putdata(error)
        else:
            self.rowcount = putdata(None)

        # return the cursor object, so you can chain operations
        return self

    def copy_to(self, stream, table,
            format=None, sep=None, null=None, decode=None, columns=None):
        """Copy data from the specified table to an output stream.

        The output stream can be a file-like object with a write() method or
        it can also be None, in which case the method will return a generator
        yielding a row on each iteration.

        Output will be returned as byte strings unless you set decode to true.

        Note that you can also use a select query instead of the table name.

        The format must be text, csv or binary. The sep option sets the
        column separator (delimiter) used in the non binary formats.
        The null option sets the textual representation of NULL in the output.

        The copy operation can be restricted to a subset of columns. If no
        columns are specified, all of them will be copied.
        """
        binary_format = format == 'binary'
        if stream is not None:
            try:
                write = stream.write
            except AttributeError:
                raise TypeError("Need an output stream to copy to")
        if not table or not isinstance(table, basestring):
            raise TypeError("Need a table to copy to")
        if table.lower().startswith('select'):
            if columns:
                raise ValueError("Columns must be specified in the query")
            table = '(%s)' % (table,)
        else:
            table = '"%s"' % (table,)
        operation = ['copy %s' % (table,)]
        options = []
        params = []
        if format is not None:
            if not isinstance(format, basestring):
                raise TypeError("The format option must be a string")
            if format not in ('text', 'csv', 'binary'):
                raise ValueError("Invalid format")
            options.append('format %s' % (format,))
        if sep is not None:
            if not isinstance(sep, basestring):
                raise TypeError("The sep option must be a string")
            if binary_format:
                raise ValueError(
                    "The sep option is not allowed with binary format")
            if len(sep) != 1:
                raise ValueError(
                    "The sep option must be a single one-byte character")
            options.append('delimiter %s')
            params.append(sep)
        if null is not None:
            if not isinstance(null, basestring):
                raise TypeError("The null option must be a string")
            options.append('null %s')
            params.append(null)
        if decode is None:
            if format == 'binary':
                decode = False
            else:
                decode = str is unicode
        else:
            if not isinstance(decode, (int, bool)):
                raise TypeError("The decode option must be a boolean")
            if decode and binary_format:
                raise ValueError(
                    "The decode option is not allowed with binary format")
        if columns:
            if not isinstance(columns, basestring):
                columns = ','.join('"%s"' % (col,) for col in columns)
            operation.append('(%s)' % (columns,))

        operation.append("to stdout")
        if options:
            operation.append('(%s)' % ','.join(options))
        operation = ' '.join(operation)

        getdata = self._src.getdata
        self.execute(operation, params)

        def copy():
            self.rowcount = 0
            while True:
                row = getdata(decode)
                if isinstance(row, int):
                    if self.rowcount != row:
                        self.rowcount = row
                    break
                self.rowcount += 1
                yield row

        if stream is None:
            # no input stream, return the generator
            return copy()

        # write the rows to the file-like input stream
        for row in copy():
            write(row)

        # return the cursor object, so you can chain operations
        return self

    def __next__(self):
        """Return the next row (support for the iteration protocol)."""
        res = self.fetchone()
        if res is None:
            raise StopIteration
        return res

    # Note that since Python 2.6 the iterator protocol uses __next()__
    # instead of next(), we keep it only for backward compatibility of pgdb.
    next = __next__

    @staticmethod
    def nextset():
        """Not supported."""
        raise NotSupportedError("The nextset() method is not supported")

    @staticmethod
    def setinputsizes(sizes):
        """Not supported."""
        pass  # unsupported, but silently passed

    @staticmethod
    def setoutputsize(size, column=0):
        """Not supported."""
        pass  # unsupported, but silently passed

    @staticmethod
    def row_factory(row):
        """Process rows before they are returned.

        You can overwrite this statically with a custom row factory, or
        you can build a row factory dynamically with build_row_factory().

        For example, you can create a Cursor class that returns rows as
        Python dictionaries like this:

            class DictCursor(pgdb.Cursor):

                def row_factory(self, row):
                    return {desc[0]: value
                        for desc, value in zip(self.description, row)}

            cur = DictCursor(con)  # get one DictCursor instance or
            con.cursor_type = DictCursor  # always use DictCursor instances
        """
        raise NotImplementedError

    def build_row_factory(self):
        """Build a row factory based on the current description.

        This implementation builds a row factory for creating named tuples.
        You can overwrite this method if you want to dynamically create
        different row factories whenever the column description changes.
        """
        colnames = self.colnames
        if colnames:
            try:
                try:
                    return namedtuple('Row', colnames, rename=True)._make
                except TypeError:  # Python 2.6 and 3.0 do not support rename
                    colnames = [v if v.isalnum() else 'column_%d' % n
                             for n, v in enumerate(colnames)]
                    return namedtuple('Row', colnames)._make
            except ValueError:  # there is still a problem with the field names
                colnames = ['column_%d' % n for n in range(len(colnames))]
                return namedtuple('Row', colnames)._make


CursorDescription = namedtuple('CursorDescription',
    ['name', 'type_code', 'display_size', 'internal_size',
     'precision', 'scale', 'null_ok'])


### Connection Objects

class Connection(object):
    """Connection object."""

    # expose the exceptions as attributes on the connection object
    Error = Error
    Warning = Warning
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    InternalError = InternalError
    OperationalError = OperationalError
    ProgrammingError = ProgrammingError
    IntegrityError = IntegrityError
    DataError = DataError
    NotSupportedError = NotSupportedError

    def __init__(self, cnx):
        """Create a database connection object."""
        self._cnx = cnx  # connection
        self._tnx = False  # transaction state
        self.type_cache = TypeCache(cnx)
        self.cursor_type = Cursor
        try:
            self._cnx.source()
        except Exception:
            raise _op_error("Invalid connection")

    def __enter__(self):
        """Enter the runtime context for the connection object.

        The runtime context can be used for running transactions.
        """
        return self

    def __exit__(self, et, ev, tb):
        """Exit the runtime context for the connection object.

        This does not close the connection, but it ends a transaction.
        """
        if et is None and ev is None and tb is None:
            self.commit()
        else:
            self.rollback()

    def close(self):
        """Close the connection object."""
        if self._cnx:
            if self._tnx:
                try:
                    self.rollback()
                except DatabaseError:
                    pass
            self._cnx.close()
            self._cnx = None
        else:
            raise _op_error("Connection has been closed")

    def commit(self):
        """Commit any pending transaction to the database."""
        if self._cnx:
            if self._tnx:
                self._tnx = False
                try:
                    self._cnx.source().execute("COMMIT")
                except DatabaseError:
                    raise
                except Exception:
                    raise _op_error("Can't commit")
        else:
            raise _op_error("Connection has been closed")

    def rollback(self):
        """Roll back to the start of any pending transaction."""
        if self._cnx:
            if self._tnx:
                self._tnx = False
                try:
                    self._cnx.source().execute("ROLLBACK")
                except DatabaseError:
                    raise
                except Exception:
                    raise _op_error("Can't rollback")
        else:
            raise _op_error("Connection has been closed")

    def cursor(self):
        """Return a new cursor object using the connection."""
        if self._cnx:
            try:
                return self.cursor_type(self)
            except Exception:
                raise _op_error("Invalid connection")
        else:
            raise _op_error("Connection has been closed")

    if shortcutmethods:  # otherwise do not implement and document this

        def execute(self, operation, params=None):
            """Shortcut method to run an operation on an implicit cursor."""
            cursor = self.cursor()
            cursor.execute(operation, params)
            return cursor

        def executemany(self, operation, param_seq):
            """Shortcut method to run an operation against a sequence."""
            cursor = self.cursor()
            cursor.executemany(operation, param_seq)
            return cursor


### Module Interface

_connect_ = connect

def connect(dsn=None,
        user=None, password=None,
        host=None, database=None):
    """Connect to a database."""
    # first get params from DSN
    dbport = -1
    dbhost = ""
    dbbase = ""
    dbuser = ""
    dbpasswd = ""
    dbopt = ""
    try:
        params = dsn.split(":")
        dbhost = params[0]
        dbbase = params[1]
        dbuser = params[2]
        dbpasswd = params[3]
        dbopt = params[4]
    except (AttributeError, IndexError, TypeError):
        pass

    # override if necessary
    if user is not None:
        dbuser = user
    if password is not None:
        dbpasswd = password
    if database is not None:
        dbbase = database
    if host is not None:
        try:
            params = host.split(":")
            dbhost = params[0]
            dbport = int(params[1])
        except (AttributeError, IndexError, TypeError, ValueError):
            pass

    # empty host is localhost
    if dbhost == "":
        dbhost = None
    if dbuser == "":
        dbuser = None

    # open the connection
    cnx = _connect_(dbbase, dbhost, dbport, dbopt, dbuser, dbpasswd)
    return Connection(cnx)


### Types Handling

class Type(frozenset):
    """Type class for a couple of PostgreSQL data types.

    PostgreSQL is object-oriented: types are dynamic.
    We must thus use type names as internal type codes.
    """

    def __new__(cls, values):
        if isinstance(values, basestring):
            values = values.split()
        return super(Type, cls).__new__(cls, values)

    def __eq__(self, other):
        if isinstance(other, basestring):
            if other.startswith('_'):
                other = other[1:]
            return other in self
        else:
            return super(Type, self).__eq__(other)

    def __ne__(self, other):
        if isinstance(other, basestring):
            if other.startswith('_'):
                other = other[1:]
            return other not in self
        else:
            return super(Type, self).__ne__(other)


class ArrayType:
    """Type class for PostgreSQL array types."""

    def __eq__(self, other):
        if isinstance(other, basestring):
            return other.startswith('_')
        else:
            return isinstance(other, ArrayType)

    def __ne__(self, other):
        if isinstance(other, basestring):
            return not other.startswith('_')
        else:
            return not isinstance(other, ArrayType)


class RecordType:
    """Type class for PostgreSQL record types."""

    def __eq__(self, other):
        if isinstance(other, TypeCode):
            return other.type == 'c'
        elif isinstance(other, basestring):
            return other == 'record'
        else:
            return isinstance(other, RecordType)

    def __ne__(self, other):
        if isinstance(other, TypeCode):
            return other.type != 'c'
        elif isinstance(other, basestring):
            return other != 'record'
        else:
            return not isinstance(other, RecordType)


# Mandatory type objects defined by DB-API 2 specs:

STRING = Type('char bpchar name text varchar')
BINARY = Type('bytea')
NUMBER = Type('int2 int4 serial int8 float4 float8 numeric money')
DATETIME = Type('date time timetz timestamp timestamptz interval'
    ' abstime reltime')  # these are very old
ROWID = Type('oid')


# Additional type objects (more specific):

BOOL = Type('bool')
SMALLINT = Type('int2')
INTEGER = Type('int2 int4 int8 serial')
LONG = Type('int8')
FLOAT = Type('float4 float8')
NUMERIC = Type('numeric')
MONEY = Type('money')
DATE = Type('date')
TIME = Type('time timetz')
TIMESTAMP = Type('timestamp timestamptz')
INTERVAL = Type('interval')
JSON = Type('json jsonb')

# Type object for arrays (also equate to their base types):

ARRAY = ArrayType()

# Type object for records (encompassing all composite types):

RECORD = RecordType()


# Mandatory type helpers defined by DB-API 2 specs:

def Date(year, month, day):
    """Construct an object holding a date value."""
    return date(year, month, day)


def Time(hour, minute=0, second=0, microsecond=0):
    """Construct an object holding a time value."""
    return time(hour, minute, second, microsecond)


def Timestamp(year, month, day, hour=0, minute=0, second=0, microsecond=0):
    """Construct an object holding a time stamp value."""
    return datetime(year, month, day, hour, minute, second, microsecond)


def DateFromTicks(ticks):
    """Construct an object holding a date value from the given ticks value."""
    return Date(*localtime(ticks)[:3])


def TimeFromTicks(ticks):
    """Construct an object holding a time value from the given ticks value."""
    return Time(*localtime(ticks)[3:6])


def TimestampFromTicks(ticks):
    """Construct an object holding a time stamp from the given ticks value."""
    return Timestamp(*localtime(ticks)[:6])


class Binary(bytes):
    """Construct an object capable of holding a binary (long) string value."""


# Additional type helpers for PyGreSQL:

class Bytea(bytes):
    """Construct an object capable of holding a bytea value."""


class Json:
    """Construct a wrapper for holding an object serializable to JSON."""

    def __init__(self, obj, encode=None):
        self.obj = obj
        self.encode = encode or jsonencode

    def __str__(self):
        obj = self.obj
        if isinstance(obj, basestring):
            return obj
        return self.encode(obj)

    __pg_repr__ = __str__


class Literal:
    """Construct a wrapper for holding a literal SQL string."""

    def __init__(self, sql):
        self.sql = sql

    def __str__(self):
        return self.sql

    __pg_repr__ = __str__


# If run as script, print some information:

if __name__ == '__main__':
    print('PyGreSQL version', version)
    print('')
    print(__doc__)
