

import inspect
import logging
import os
import pickle
import random
import shutil
import sys
import time

from collections import defaultdict
from pickle import UnpicklingError
from threading import Lock
from six import with_metaclass


logger = logging.getLogger(__name__)

CURRENT_PATH = os.path.realpath(__file__)
CACHE_LOCATION = os.path.join(CURRENT_PATH, '../../../_cache')

SIZE_LIMIT = 1000000000  # 1G bytes, max disk usage allowed by cache
ENTRY_LIMIT = 1000000    # Max number of pickle files allowed in cache.
DISABLE_CACHE_PARAM = "disable_cache"


class Singleton(type):

    _instances = {}
    _lock = Lock()

    def __call__(cls, *args, **kwargs):
        with cls._lock:
            if cls not in cls._instances:
                instance = super(Singleton, cls).__call__(*args, **kwargs)
                cls._instances[cls] = instance
        return cls._instances[cls]


class FactsCache(with_metaclass(Singleton, object)):
    """Singleton class for reading from cache and write to cache.

    Used singleton design pattern. Only a single instance of this class can be initialized.

    Args:
        with_metaclass ([function]): Python 2&3 compatible function from the six library for adding metaclass.
    """

    NOTEXIST = object()

    def __init__(self, cache_location=CACHE_LOCATION):
        self._cache_location = os.path.abspath(cache_location)
        self._cache = defaultdict(dict)
        self._write_lock = Lock()

    def _check_usage(self):
        """Check cache usage, raise exception if usage exceeds the limitations.
        """
        total_size = 0
        total_entries = 0
        for root, _, files in os.walk(self._cache_location):
            for f in files:
                fp = os.path.join(root, f)
                total_size += os.path.getsize(fp)
                total_entries += 1

        if total_size > SIZE_LIMIT or total_entries > ENTRY_LIMIT:
            msg = 'Cache usage exceeds limitations. total_size={}, SIZE_LIMIT={}, total_entries={}, ENTRY_LIMIT={}' \
                .format(total_size, SIZE_LIMIT, total_entries, ENTRY_LIMIT)
            raise Exception(msg)

    def _read_facts_file(self, facts_file, z, k):
        with open(facts_file, 'rb') as f:
            self._cache[z][k] = pickle.load(f)
            logger.debug('[Cache] Loaded cached facts "{}.{}" from {}'.format(z, k, facts_file))
            return self._cache[z][k]

    def read(self, zone, key):
        """Read cached facts.

        Args:
            zone (str): Cached facts are organized by zones. This argument is to specify the zone name.
                The zone name could be hostname.
            key (str): Name of cached facts.

        Returns:
            obj: Cached object, usually a dictionary.
        """
        # Lazy load
        if zone in self._cache and key in self._cache[zone]:
            logger.debug('[Cache] Read cached facts "{}.{}"'.format(zone, key))
            return self._cache[zone][key]
        else:
            facts_file = os.path.join(self._cache_location, '{}/{}.pickle'.format(zone, key))
            try:
                return self._read_facts_file(facts_file, zone, key)
            except (IOError, ValueError) as e:
                logger.info('[Cache] Load cache file "{}" failed with IOError or ValueError: {}'
                            .format(os.path.abspath(facts_file), repr(e)))
                return self.NOTEXIST
            except (EOFError, UnpicklingError) as e:
                # When parallel run is enabled, multiple processes may try to read/write the same cache file,
                # so there will be a chance that
                #   - a file is being written by process1 while process2 is reading it, causing EOFError in process2
                #   - a file is being read by multiple processes at the same time, causing UnpicklingError in some of
                #     the processes
                # In these cases, we will retry to read the file after a short random sleep. If we still get the same
                # error after retrying, we will return NOTEXIST to overwrite the file.
                retry_attempts = 3
                for attempt in range(retry_attempts):
                    time.sleep(random.randint(3, 6))
                    try:
                        return self._read_facts_file(facts_file, zone, key)
                    except (EOFError, UnpicklingError):
                        logger.warning('[Cache] Retry {}/{} failed for file "{}"'
                                       .format(attempt + 1, retry_attempts, facts_file))

                logger.error('[Cache] Load cache file "{}" failed with EOFError or UnpicklingError: {}'
                             .format(facts_file, repr(e)))
                return self.NOTEXIST
            except Exception as e:
                logger.info('[Cache] Load cache file "{}" failed with unknown exception: {}'
                            .format(os.path.abspath(facts_file), repr(e)))
                return self.NOTEXIST

    def write(self, zone, key, value):
        """Store facts to cache.

        Args:
            zone (str): Cached facts are organized by zones. This argument is to specify the zone name.
                The zone name could be hostname.
            key (str): Name of cached facts.
            value (obj): Value of cached facts. Usually a dictionary.

        Returns:
            boolean: Caching facts is successful or not.
        """
        with self._write_lock:
            self._check_usage()
            facts_file = os.path.join(self._cache_location, '{}/{}.pickle'.format(zone, key))
            try:
                cache_subfolder = os.path.join(self._cache_location, zone)
                if not os.path.exists(cache_subfolder):
                    logger.info('[Cache] Create cache dir {}'.format(cache_subfolder))
                    os.makedirs(cache_subfolder)

                with open(facts_file, 'wb') as f:
                    pickle.dump(value, f, pickle.HIGHEST_PROTOCOL)
                    self._cache[zone][key] = value
                    logger.info('[Cache] Cached facts "{}.{}" to {}'.format(zone, key, facts_file))
                    return True
            except (IOError, ValueError) as e:
                logger.error('[Cache] Dump cache file "{}" failed with exception: {}'.format(facts_file, repr(e)))
                return False

    def cleanup(self, zone=None, key=None):
        """Cleanup cached files.

        Args:
            zone (str): Cached facts are organized by zones. This argument is to specify the zone name.
                The zone name could be hostname. Default to None. When zone is not specified, all the cached facts
                will be cleaned up.
            key (str): Name of cached facts. Default is None.
        """
        if zone:
            if key:
                if zone in self._cache and key in self._cache[zone]:
                    del self._cache[zone][key]
                    logger.debug('[Cache] Removed "{}.{}" from cache.'.format(zone, key))
                try:
                    cache_file = os.path.join(self._cache_location, zone, '{}.pickle'.format(key))
                    os.remove(cache_file)
                    logger.debug('[Cache] Removed cache file "{}.pickle"'.format(cache_file))
                except OSError as e:
                    logger.error('[Cache] Cleanup cache {}.{}.pickle failed with exception: {}'
                                 .format(zone, key, repr(e)))
            else:
                if zone in self._cache:
                    del self._cache[zone]
                    logger.debug('[Cache] Removed zone "{}" from cache'.format(zone))
                try:
                    cache_subfolder = os.path.join(self._cache_location, zone)
                    shutil.rmtree(cache_subfolder)
                    logger.debug('[Cache] Removed cache subfolder "{}"'.format(cache_subfolder))
                except OSError as e:
                    logger.error('[Cache] Remove cache subfolder "{}" failed with exception: {}'.format(zone, repr(e)))
        else:
            self._cache = defaultdict(dict)
            try:
                shutil.rmtree(self._cache_location)
                logger.debug('[Cache] Removed all cache files under "{}"'.format(self._cache_location))
            except OSError as e:
                logger.error('[Cache] Remove cache folder "{}" failed with exception: {}'
                             .format(self._cache_location, repr(e)))


def _get_default_zone(function, func_args, func_kargs):
    """
        Default zone getter used for decorator cached.
        For multi asic platforms some the facts will have the namespace to get the facts for an ASIC.
        Add the namespace to the default zone.
    """
    hostname = None
    unicode_type = str if sys.version_info.major >= 3 else unicode      # noqa: F821
    if func_args:
        hostname = getattr(func_args[0], "hostname", None)
    if not hostname or type(hostname) not in [str, unicode_type]:
        raise ValueError("Failed to get attribute 'hostname' of type string from instance of type %s."
                         % type(func_args[0]))
    zone = hostname
    signature = inspect.signature(function)
    arg_names = list(signature.parameters.keys())
    if 'namespace' in arg_names:
        try:
            index = arg_names.index('namespace')
            namespace = func_args[index]
            if namespace and isinstance(namespace, str):
                zone = "{}-{}".format(hostname, namespace)
        except IndexError:
            pass
    return zone


def _get_disable_cache(target, args, kwargs):
    """
    For the function with signature:

        @cached(name='feature_status')
        def get_feature_status(self, disable_cache=True):

    If the disable_cache is not explicitly passed, like it get called by .get_feature_status()
    disable_cache will not show in **kwargs,
    Need to fetch it with inspect.
    """
    # Get the function signature
    sig = inspect.signature(target)
    bound_args = sig.bind_partial(*args, **kwargs)
    bound_args.apply_defaults()

    return bound_args.arguments.get(DISABLE_CACHE_PARAM, False)


def cached(name, zone_getter=None, after_read=None, before_write=None):
    """Decorator for enabling cache for facts.

    The cached facts are to be stored by <name>.pickle. Because the cached pickle files must be stored under subfolder
    specified by zone, the decorate have an option to passed a zone getter function used to get zone. The zone getter
    function must have signature of '(function, func_args, func_kargs)' that 'function' is the decorated function,
    'func_args' and 'func_kargs' are the parameters passed to the decorated function at runtime.
    The zone getter function should raise an error if it fails to return a string as zone.
    With default zone getter function, this decorator can try to find zone:
    if the function is a bound method of class AnsibleHostBase and its derivatives, it will try to use its
    attribute 'hostname' as zone, or raises an error if 'hostname' doesn't exists or is not a string.

    Args:
        name ([str]): Name of the cached facts.
        zone_getter ([function]): Function used to get hostname used as zone.
        after_read ([function]): Hook function used to process facts after read from cache.
        before_write ([function]): Hook function used to process facts before write into cache.
    Returns:
        [function]: Decorator function.
    """
    cache = FactsCache()

    def decorator(target):
        def wrapper(*args, **kargs):

            # Support to choose enable/disable cache by function param
            disable_cache = _get_disable_cache(target, args, kargs)
            if disable_cache:
                return target(*args, **kargs)

            _zone_getter = zone_getter or _get_default_zone
            zone = _zone_getter(target, args, kargs)

            cached_facts = cache.read(zone, name)
            if after_read:
                cached_facts = after_read(cached_facts, target, args, kargs)
            if cached_facts is not FactsCache.NOTEXIST:
                logger.debug(f"[Cache] Use cache for func[{target}], zone[{zone}], key[{name}]")
                return cached_facts
            else:
                facts = target(*args, **kargs)
                if before_write:
                    _facts = before_write(facts, target, args, kargs)
                    cache.write(zone, name, _facts)
                else:
                    cache.write(zone, name, facts)
                return facts
        return wrapper
    return decorator


if __name__ == '__main__':
    cache = FactsCache()
    if len(sys.argv) == 2:
        zone = sys.argv[1]
    else:
        zone = None
    cache.cleanup(zone)
