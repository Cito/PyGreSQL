# pg.py
# Written by D'Arcy J.M. Cain

# This library implements some basic database management stuff.  It
# includes the pg module and builds on it.  This is known as the
# "Classic" interface.  For DB-API compliance use the pgdb module.

from _pg import *
from types import *
import string, re, sys

# utility function
# We expect int, seq, decimal, text or date (more later)
def _quote(d, t):
	if d == None:
		return "NULL"

	if t in ['int', 'seq']:
		if d == "": return "NULL"
		return "%d" % long(d)

	if t == 'decimal':
		if d == "": return "NULL"
		return "%f" % float(d)

	if t == 'money':
		if d == "": return "NULL"
		return "'%.2f'" % float(d)

	if t == 'bool':
		# Can't run upper() on these
		if d in (0, 1): return ("'f'", "'t'")[d]

		if string.upper(d) in ['T', 'TRUE', 'Y', 'YES', '1', 'ON']:
			return "'t'"
		else:
			return "'f'"

	if t == 'date' and d == '': return "NULL"
	if t in ('inet', 'cidr') and d == '': return "NULL"

	return "'%s'" % string.strip(re.sub("'", "''", \
							 re.sub("\\\\", "\\\\\\\\", "%s" % d)))

class DB:
	"""This class wraps the pg connection type"""

	def __init__(self, *args, **kw):
		self.db = connect(*args, **kw)

		# Create convenience methods, in a way that is still overridable
		# (members are not copied because they are actually functions)
		for e in self.db.__methods__:
			if e not in ("close", "query"):	# These are wrapped separately
				setattr(self, e, getattr(self.db, e))

		self.__attnames = {}
		self.__pkeys = {}
		self.__args = args, kw
		self.debug = None	# For debugging scripts, this can be set to a
							# string format specification (e.g. in a CGI
							# set to "%s<BR>",) to a function which takes
							# a string argument or a file object to write
							# debug statements to.

	def _do_debug(self, s):
		if not self.debug: return
		if isinstance(self.debug, StringType): print self.debug % s
		if isinstance(self.debug, FunctionType): self.debug(s)
		if isinstance(self.debug, FileType): print >> self.debug, s

	# wrap close so we can track state
	def close(self,):
		self.db.close()
		self.db = None

	# in case we need another connection to the same database
	# note that we can still reopen a database that we have closed
	def reopen(self):
		if self.db: self.close()
		try: self.db = connect(*self.__args[0], **self.__args[1])
		except:
			self.db = None
			raise

	# wrap query for debugging
	def query(self, qstr):
		self._do_debug(qstr)
		return self.db.query(qstr)

	def pkey(self, cl, newpkey = None):
		"""This method returns the primary key of a class.  If newpkey
			is set and is set and is not a dictionary then set that
			value as the primary key of the class.  If it is a dictionary
			then replace the __pkeys dictionary with it."""
		# Get all the primary keys at once
		if isinstance(newpkey, DictType):
			self.__pkeys = newpkey
			return

		if newpkey:
			self.__pkeys[cl] = newpkey
			return newpkey

		if self.__pkeys == {}:
			for rel, att in self.db.query("""SELECT
							pg_class.relname, pg_attribute.attname
						FROM pg_class, pg_attribute, pg_index
						WHERE pg_class.oid = pg_attribute.attrelid AND
							pg_class.oid = pg_index.indrelid AND
							pg_index.indkey[0] = pg_attribute.attnum AND 
							pg_index.indisprimary = 't' AND
							pg_attribute.attisdropped = 'f'""").getresult():
				self.__pkeys[rel] = att
		# Give it one more chance in case it was added after we started
		elif not self.__pkeys.has_key(cl):
			self.__pkeys = {}
			return self.pkey(cl)

		# will raise an exception if primary key doesn't exist
		return self.__pkeys[cl]

	def get_databases(self):
		return [x[0] for x in
			self.db.query("SELECT datname FROM pg_database").getresult()]

	def get_tables(self):
		return [x[0] for x in
				self.db.query("""SELECT relname FROM pg_class
						WHERE relkind = 'r' AND
							relname !~ '^Inv' AND
							relname !~ '^pg_'""").getresult()]

	def get_attnames(self, cl, newattnames = None):
		"""This method gets a list of attribute names for a class.  If
			the optional newattnames exists it must be a dictionary and
			will become the new attribute names dictionary."""

		if isinstance(newattnames, DictType):
			self.__attnames = newattnames
			return
		elif newattnames:
			raise ProgrammingError, \
					"If supplied, newattnames must be a dictionary"

		# May as well cache them
		if self.__attnames.has_key(cl):
			return self.__attnames[cl]

		query = """SELECT pg_attribute.attname, pg_type.typname
					FROM pg_class, pg_attribute, pg_type
					WHERE pg_class.relname = '%s' AND
						pg_attribute.attnum > 0 AND
						pg_attribute.attrelid = pg_class.oid AND
						pg_attribute.atttypid = pg_type.oid AND
						pg_attribute.attisdropped = 'f'"""

		l = {}
		for attname, typname in self.db.query(query % cl).getresult():
			if re.match("^interval", typname):
				l[attname] = 'text'
			if re.match("^int", typname):
				l[attname] = 'int'
			elif re.match("^oid", typname):
				l[attname] = 'int'
			elif re.match("^text", typname):
				l[attname] = 'text'
			elif re.match("^char", typname):
				l[attname] = 'text'
			elif re.match("^name", typname):
				l[attname] = 'text'
			elif re.match("^abstime", typname):
				l[attname] = 'date'
			elif re.match("^date", typname):
				l[attname] = 'date'
			elif re.match("^timestamp", typname):
				l[attname] = 'date'
			elif re.match("^bool", typname):
				l[attname] = 'bool'
			elif re.match("^float", typname):
				l[attname] = 'decimal'
			elif re.match("^money", typname):
				l[attname] = 'money'
			else:
				l[attname] = 'text'

		l['oid'] = 'int'				# every table has this
		self.__attnames[cl] = l		# cache it
		return self.__attnames[cl]

	# return a tuple from a database
	def get(self, cl, arg, keyname = None, view = 0):
		if cl[-1] == '*':			# need parent table name
			xcl = cl[:-1]
		else:
			xcl = cl

		if keyname == None:			# use the primary key by default
			keyname = self.pkey(xcl)

		fnames = self.get_attnames(xcl)

		if isinstance(arg, DictType):
			# To allow users to work with multiple tables we munge the
			# name when the key is "oid"
			if keyname == 'oid': k = arg['oid_%s' % xcl]
			else: k = arg[keyname]
		else:
			k = arg
			arg = {}

		# We want the oid for later updates if that isn't the key
		if keyname == 'oid':
			q = "SELECT * FROM %s WHERE oid = %s" % (cl, k)
		elif view:
			q = "SELECT * FROM %s WHERE %s = %s" % \
				(cl, keyname, _quote(k, fnames[keyname]))
		else:
			q = "SELECT oid AS oid_%s, %s FROM %s WHERE %s = %s" % \
				(xcl, string.join(fnames.keys(), ','),\
					cl, keyname, _quote(k, fnames[keyname]))

		self._do_debug(q)
		res = self.db.query(q).dictresult()
		if res == []:
			raise DatabaseError, \
				"No such record in %s where %s is %s" % \
								(cl, keyname, _quote(k, fnames[keyname]))
			return None

		for k in res[0].keys():
			arg[k] = res[0][k]

		return arg

	# Inserts a new tuple into a table
	# We currently don't support insert into views although PostgreSQL does
	def insert(self, cl, a):
		fnames = self.get_attnames(cl)
		l = []
		n = []
		for f in fnames.keys():
			if f != 'oid' and a.has_key(f):
				l.append(_quote(a[f], fnames[f]))
				n.append(f)

		q = "INSERT INTO %s (%s) VALUES (%s)" % \
			(cl, string.join(n, ','), string.join(l, ','))
		self._do_debug(q)
		a['oid_%s' % cl] = self.db.query(q)

		# reload the dictionary to catch things modified by engine
		# note that get() changes 'oid' below to oid_table
		# if no read perms (it can and does happen) return None
		try: return self.get(cl, a, 'oid')
		except: return None

	# Update always works on the oid which get returns if available
	# otherwise use the primary key.  Fail if neither.
	def update(self, cl, a):
		self.pkey(cl)		# make sure we have a self.__pkeys dictionary

		foid = 'oid_%s' % cl
		if a.has_key(foid):
			where = "oid = %s" % a[foid]
		elif self.__pkeys.has_key(cl) and a.has_key(self.__pkeys[cl]):
			where = "%s = '%s'" % (self.__pkeys[cl], a[self.__pkeys[cl]])
		else:
			raise ProgrammingError, \
					"Update needs primary key or oid as %s" % foid

		v = []
		k = 0
		fnames = self.get_attnames(cl)

		for ff in fnames.keys():
			if ff != 'oid' and a.has_key(ff):
				v.append("%s = %s" % (ff, _quote(a[ff], fnames[ff])))

		if v == []:
			return None

		q = "UPDATE %s SET %s WHERE %s" % (cl, string.join(v, ','), where)
		self._do_debug(q)
		self.db.query(q)

		# reload the dictionary to catch things modified by engine
		if a.has_key(foid):
			return self.get(cl, a, 'oid')
		else:
			return self.get(cl, a)

	# At some point we will need a way to get defaults from a table
	def clear(self, cl, a = {}):
		fnames = self.get_attnames(cl)
		for ff in fnames.keys():
			if fnames[ff] in ['int', 'decimal', 'seq', 'money']:
				a[ff] = 0
			else:
				a[ff] = ""

		a['oid'] = 0
		return a

	# Like update, delete works on the oid
	# one day we will be testing that the record to be deleted
	# isn't referenced somewhere (or else PostgreSQL will)
	def delete(self, cl, a):
		q = "DELETE FROM %s WHERE oid = %s" % (cl, a['oid_%s' % cl])
		self._do_debug(q)
		self.db.query(q)

