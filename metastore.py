import rpyc
import sys
import copy

'''
A sample ErrorResponse class. Use this to respond to client requests when the request has any of the following issues - 
1. The file being modified has missing blocks in the block store.
2. The file being read/deleted does not exist.
3. The request for modifying/deleting a file has the wrong file version.

The MetadataStore RPC server class.

The MetadataStore process maintains the mapping of filenames to hashlists. All
metadata is stored in memory, and no database systems or files will be used to
maintain the data.

'''


class ErrorResponse(Exception):
    def __init__(self, message):
        super(ErrorResponse, self).__init__(message)
        self.error = message

    def missing_blocks(self, hashlist):
        self.error_type = 1
        self.missing_blocks = hashlist

    def wrong_version_error(self, version):
        self.error_type = 2
        self.current_version = version

    def file_not_found(self):
        self.error_type = 3


class MetadataStore(rpyc.Service):
    """
        Initialize the class using the config file provided and also initialize
        any datastructures you may need.
    """

    def __init__(self, config):
        self.filename_hashlist = dict()  # str -> [[hashkey, blocklocation],[],[],[],..]
        self.filename_version = dict()  # str -> int

        self.tombstone_filename_version = dict()  # str -> int
        '''
            When file is deleted, it is removed from filename_hashlist and filename_version;
            It is added into tombstone_filename_version with associated with its latest version
        '''

        configuration = parse_config(config)
        self.no_of_block_stores = int(configuration[0])
        self.metadata = configuration[1]
        self.blockstores = configuration[2]

        # build connection pool of connections with all blockstores
        self.blockstore_conns = connection_to(self.blockstores)  # list of connections with every blockstore

    '''
        ModifyFile(f,v,hl): Modifies file f so that it now contains the
        contents refered to by the hashlist hl.  The version provided, v, must
        be exactly one larger than the current version that the MetadataStore
        maintains.
    
        As per rpyc syntax, adding the prefix 'exposed_' will expose this
        method as an RPC call
    '''

    def exposed_modify_file(self, filename, version, hashlist):
        # make a local copy!
        # hashlist = list(hashlist)

        # Very IMPORTANT! For more complex data structure, list() is not enough for copying
        # Use deepcopy and don't forget to set 'allow_pickle: True' in configuration
        hashlist = copy.deepcopy(hashlist)

        # check version
        if filename in self.filename_version and int(version) != self.filename_version[filename] + 1:
            error = ErrorResponse("Version Error")
            error.wrong_version_error(self.filename_version[filename])
            raise error

        # gather missing blocks
        missing_block_list = list()

        for hashnode in hashlist:
            server_no = hashnode[1]
            conn = self.blockstore_conns[server_no]
            if not conn.root.has_block(hashnode[0]):
                missing_block_list.append(hashnode[0])

        if len(missing_block_list) == 0:
            # modify filename -> version
            if filename in self.filename_hashlist:
                self.filename_version[filename] += 1
            elif filename in self.tombstone_filename_version:
                self.filename_version[filename] = self.tombstone_filename_version[filename] + 1
                del self.tombstone_filename_version[filename]
            else:
                self.filename_version[filename] = 1

            # modify filename -> hashlist
            self.filename_hashlist[filename] = hashlist
            # print(self.filename_hashlist)
            return 0
        else:
            error = ErrorResponse("Missing Block")
            error.missing_blocks(missing_block_list)
            # error.missing_blocks(tuple(missing_block_list))
            raise error

    '''
        DeleteFile(f,v): Deletes file f. Like ModifyFile(), the provided
        version number v must be one bigger than the most up-date-date version.
    
        As per rpyc syntax, adding the prefix 'exposed_' will expose this
        method as an RPC call
    '''

    def exposed_delete_file(self, filename, version):
        # check version
        if filename in self.filename_version:
            # print("1," + str(version) + " " + str(self.filename_version[filename]))
            if int(version) != self.filename_version[filename] + 1:
                error = ErrorResponse("Version Error")
                error.wrong_version_error(self.filename_version[filename])
                raise error
            self.tombstone_filename_version[filename] = self.filename_version[filename] + 1
            del self.filename_hashlist[filename]
            del self.filename_version[filename]
            return self.tombstone_filename_version[filename]
        elif filename in self.tombstone_filename_version:
            # print("2," + str(version) + " " + str(self.tombstone_filename_version[filename]))
            if int(version) != self.tombstone_filename_version[filename] + 1:
                error = ErrorResponse("Version Error")
                error.wrong_version_error(self.tombstone_filename_version[filename])
                raise error
            self.tombstone_filename_version[filename] += 1
            return self.tombstone_filename_version[filename]
        else:
            error = ErrorResponse("Not Found")
            raise error

    '''
        (v,hl) = ReadFile(f): Reads the file with filename f, returning the
        most up-to-date version number v, and the corresponding hashlist hl. If
        the file does not exist, v will be 0.
    
        As per rpyc syntax, adding the prefix 'exposed_' will expose this
        method as an RPC call
    '''

    def exposed_read_file(self, filename):
        if filename in self.filename_hashlist:
            version = self.filename_version[filename]
            hashlocation = self.filename_hashlist[filename]
            return version, hashlocation
        elif filename in self.tombstone_filename_version:
            version = self.tombstone_filename_version[filename]
            return version, list()
        else:
            return 0, list()


def parse_config(config):
    # read config file
    with open(config, "r") as text:
        lines = text.readlines()

    '''
        format:
            B: 1
            metadata: localhost:6000
            block0: localhost:5000
    '''

    # parse config file and extract parameters
    no_of_block_stores = int(lines[0].split(": ")[-1])
    metadata = dict()
    metadata["host"] = lines[1].split(": ")[1].split(":")[0]  # type: str
    metadata["port"] = lines[1].split(": ")[1].split(":")[1]  # type: str
    if metadata["host"][-1] == '\n':
        metadata["host"] = metadata["host"][:-1]
    if metadata["port"][-1] == '\n':
        metadata["port"] = metadata["port"][:-1]

    blockstores = list()  # a list of dicts
    # e.g., blockstores = [{'host': 'localhost', 'port': '6000'}, {'host': 'localhost', 'port': '6000'}]

    # extract host:port information of every blockstore server
    for i in range(no_of_block_stores):
        line = lines[2 + i]
        t = dict()
        t["host"] = line.split(": ")[1].split(":")[0]
        if t["host"][-1] == '\n':
            t["host"] = t["host"][:-1]
        t["port"] = line.split(": ")[1].split(":")[1]
        if t["port"][-1] == '\n':
            t["port"] = t["port"][:-1]
        blockstores.append(t)

    block_replacement_algorithm = int(lines[2 + no_of_block_stores].split(": ")[-1])
    return no_of_block_stores, metadata, blockstores, block_replacement_algorithm


def connection_to(servers):
    connections = list()
    for server in servers:
        host = server["host"]
        port = int(server["port"])
        conn = rpyc.connect(host, port)
        connections.append(conn)
    return connections


if __name__ == '__main__':

    with open(sys.argv[1], "r") as f:
        lines = f.readlines()
    host = lines[1].split(": ")[1].split(":")[0]
    port = lines[1].split(": ")[1].split(":")[1]
    port = port[:-1]

    from rpyc.utils.server import ThreadedServer
    server = ThreadedServer(MetadataStore(sys.argv[1]), port=int(port), 
        protocol_config={'allow_all_attrs': True, 'allow_pickle': True})
    server.start()
