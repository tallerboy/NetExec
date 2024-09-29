from nxc.connection import connection
from nxc.logger import NXCAdapter
from pyNfsClient import Portmap, Mount, NFSv3, NFS_PROGRAM, NFS_V3, ACCESS3_READ, ACCESS3_MODIFY, ACCESS3_EXECUTE
import re
import uuid
import math


class nfs(connection):
    def __init__(self, args, db, host):
        self.protocol = "nfs"
        self.port = 111
        self.portmap = None
        self.mnt_port = None
        self.mount = None
        self.uid = args.uid
        self.auth = {
            "flavor": 1,
            "machine_name": uuid.uuid4().hex.upper()[0:6],
            "uid": self.uid,
            "gid": 0,
            "aux_gid": [],
        }
        connection.__init__(self, args, db, host)

    def proto_logger(self):
        self.logger = NXCAdapter(
            extra={
                "protocol": "NFS",
                "host": self.host,
                "port": self.port,
                "hostname": self.hostname,
            }
        )

    def create_conn_obj(self):
        """Initializes and connects to the portmap and mounted folder"""
        try:
            # Portmap Initialization
            self.portmap = Portmap(self.host, timeout=self.args.nfs_timeout)
            self.portmap.connect()

            # Mount Initialization
            self.mnt_port = self.portmap.getport(Mount.program, Mount.program_version)
            self.mount = Mount(host=self.host, port=self.mnt_port, timeout=self.args.nfs_timeout, auth=self.auth)
            self.mount.connect()

            # Change logging port to the NFS port
            self.port = self.mnt_port
            self.proto_logger()
        except Exception as e:
            self.logger.fail(f"Error during Initialization: {e}")
            return False
        return True

    def enum_host_info(self):
        try:
            # Dump all registered programs
            programs = self.portmap.dump()

            self.nfs_versions = set()
            for program in programs:
                if program["program"] == NFS_PROGRAM:
                    self.nfs_versions.add(program["version"])
            return self.nfs_versions
        except Exception as e:
            self.logger.debug(f"Error checking NFS version: {self.host} {e}")

    def print_host_info(self):
        self.logger.display(f"Target supported NFS versions: ({', '.join(str(x) for x in self.nfs_versions)})")
        return True

    def disconnect(self):
        """Disconnect mount and portmap if they are connected"""
        try:
            self.mount.disconnect()
            self.portmap.disconnect()
            self.logger.info(f"Disconnect successful: {self.host}:{self.port}")
        except Exception as e:
            self.logger.fail(f"Error during disconnect: {e}")

    def list_dir(self, file_handle, path, recurse=1):
        """Process entries in NFS directory recursively"""
        def process_entries(entries, path, recurse):
            try:
                contents = []
                for entry in entries:
                    if "name" in entry and entry["name"] not in [b".", b".."]:
                        item_path = f'{path}/{entry["name"].decode("utf-8")}'  # Constructing file path
                        if entry.get("name_attributes", {}).get("present", False):
                            if entry["name_attributes"]["attributes"]["type"] == 2 and recurse > 0:  # Recursive directory listing. Entry type shows file format. 1 is file, 2 is folder.
                                dir_handle = entry["name_handle"]["handle"]["data"]
                                contents += self.list_dir(dir_handle, item_path, recurse=recurse - 1)
                            else:
                                read_perm, write_perm, exec_perm = self.get_permissions(entry["name_handle"]["handle"]["data"])
                                contents.append({"path": item_path, "read": read_perm, "write": write_perm, "execute": exec_perm})

                    if entry["nextentry"]:
                        # Processing next entries recursively
                        contents += process_entries(entry["nextentry"], path, recurse)

                return contents
            except Exception as e:
                self.logger.debug(f"Error on Listing Entries for NFS Shares: {self.host}:{self.port} {e}")

        if recurse == 0:
            read_perm, write_perm, exec_perm = self.get_permissions(file_handle)
            return [{"path": f"{path}/", "read": read_perm, "write": write_perm, "execute": exec_perm}]

        items = self.nfs3.readdirplus(file_handle, auth=self.auth)
        if "resfail" in items:
            raise Exception("Insufficient Permissions")
        else:
            entries = items["resok"]["reply"]["entries"]

        return process_entries(entries, path, recurse)

    def export_info(self, export_nodes):
        """Enumerates all NFS shares and their access range"""
        result = []
        for node in export_nodes:
            ex_dir = node.ex_dir.decode()
            # Collect the names of the groups associated with this export node
            group_names = self.group_names(node.ex_groups)
            result.append(f"{ex_dir} {', '.join(group_names)}")

            # If there are more export nodes, process them recursively. More than one share.
            if node.ex_next:
                result.extend(self.export_info(node.ex_next))
        return result

    def group_names(self, groups):
        """Enumerates all access range of the share(s)"""
        result = []
        for group in groups:
            result.append(group.gr_name.decode())

            # If there are more IP's, process them recursively.
            if group.gr_next:
                result.extend(self.group_names(group.gr_next))

        return result

    def shares(self):
        self.logger.display(f"Enumerating NFS Shares with UID {self.uid}")
        try:
            # Connect to NFS
            nfs_port = self.portmap.getport(NFS_PROGRAM, NFS_V3)
            self.nfs3 = NFSv3(self.host, nfs_port, self.args.nfs_timeout, self.auth)
            self.nfs3.connect()

            output_export = str(self.mount.export())
            reg = re.compile(r"ex_dir=b'([^']*)'")
            shares = list(reg.findall(output_export))

            # Mount shares and check permissions
            self.logger.highlight(f"{'Perms':<9}{'Storage Usage':<17}{'Share':<15}")
            self.logger.highlight(f"{'-----':<9}{'-------------':<17}{'-----':<15}")
            for share in shares:
                try:
                    mnt_info = self.mount.mnt(share, self.auth)
                    file_handle = mnt_info["mountinfo"]["fhandle"]

                    info = self.nfs3.fsstat(file_handle, self.auth)
                    free_space = info["resok"]["fbytes"]
                    total_space = info["resok"]["tbytes"]
                    used_space = total_space - free_space
                    read_perm, write_perm, exec_perm = self.get_permissions(file_handle)
                    self.mount.umnt(self.auth)
                    self.logger.highlight(f"{'r' if read_perm else '-'}{'w' if write_perm else '-'}{('x' if exec_perm else '-'):<7}{convert_size(used_space)}/{convert_size(total_space):<9} {share:<15}")
                except Exception as e:
                    self.logger.fail(f"{share} - {e}")
                    self.logger.highlight(f"{'---':<15}{share:<15}")

        except Exception as e:
            self.logger.fail(f"Error on Enumeration NFS Shares: {self.host}:{self.port} {e}")
        finally:
            self.nfs3.disconnect()

    def get_permissions(self, file_handle):
        """Check permissions for the file handle"""
        try:
            read_perm = self.nfs3.access(file_handle, ACCESS3_READ, self.auth).get("resok", {}).get("access", 0) is ACCESS3_READ
        except Exception:
            read_perm = False
        try:
            write_perm = self.nfs3.access(file_handle, ACCESS3_MODIFY, self.auth).get("resok", {}).get("access", 0) is ACCESS3_MODIFY
        except Exception:
            write_perm = False
        try:
            exec_perm = self.nfs3.access(file_handle, ACCESS3_EXECUTE, self.auth).get("resok", {}).get("access", 0) is ACCESS3_EXECUTE
        except Exception:
            exec_perm = False
        return read_perm, write_perm, exec_perm

    def enum_shares(self):
        try:
            nfs_port = self.portmap.getport(NFS_PROGRAM, NFS_V3)
            self.nfs3 = NFSv3(self.host, nfs_port, self.args.nfs_timeout, self.auth)
            self.nfs3.connect()

            # Mounting NFS Shares
            output_export = str(self.mount.export())
            reg = re.compile(r"ex_dir=b'([^']*)'")
            shares = list(reg.findall(output_export))

            self.logger.display(f"Enumerating NFS Shares Directories with UID {self.uid}")
            for share in shares:
                try:
                    mount_info = self.mount.mnt(share, self.auth)
                    contents = self.list_dir(mount_info["mountinfo"]["fhandle"], share, self.args.enum_shares)
                    self.logger.success(share)
                    for content in contents:
                        self.logger.highlight(f"{'r' if content['read'] else '-'}{'w' if content['write'] else '-'}{'x' if content['execute'] else '-'} {content['path']}")
                except Exception as e:
                    if "RPC_AUTH_ERROR: AUTH_REJECTEDCRED" in str(e):
                        self.logger.fail(f"{share} - RPC Access denied")
                    elif "RPC_AUTH_ERROR: AUTH_TOOWEAK" in str(e):
                        self.logger.fail(f"{share} - Kerberos authentication required")
                    elif "Insufficient Permissions" in str(e):
                        self.logger.fail(f"{share} - Insufficient Permissions for share listing")
                    else:
                        self.logger.exception(f"{share} - {e}")
        except Exception as e:
            self.logger.debug(f"Error on Listing NFS Shares Directories: {self.host}:{self.port} {e}")
            self.logger.debug("It is probably unknown format or can not access as anonymously.")
        finally:
            self.nfs3.disconnect()

    def uid_brute(self):
        nfs_port = self.portmap.getport(NFS_PROGRAM, NFS_V3)
        self.nfs3 = NFSv3(self.host, nfs_port, self.args.nfs_timeout, self.auth)
        self.nfs3.connect()

        # Mounting NFS Shares
        output_export = str(self.mount.export())
        reg = re.compile(r"ex_dir=b'([^']*)'")
        shares = list(reg.findall(output_export))

        self.list_exported_shares(self.args.uid_brute, shares, 1)

    def list_exported_shares(self, max_uid, shares, recurse_depth):
        if self.args.uid_brute:
            self.logger.display(f"Enumerating NFS Shares up to UID {max_uid}")
        else:
            self.logger.display(f"Enumerating NFS Shares with UID {max_uid}")
        white_list = []
        for uid in range(max_uid + 1):
            self.auth["uid"] = uid
            for share in shares:
                try:
                    if share in white_list:
                        self.logger.debug(f"Skipping {share} as it is already listed.")
                        continue
                    else:
                        mount_info = self.mount.mnt(share, self.auth)
                        contents = self.list_dir(mount_info["mountinfo"]["fhandle"], share, recurse_depth)
                        white_list.append(share)
                        self.logger.success(share)
                        for content in contents:
                            self.logger.highlight(f"\tUID: {self.auth['uid']} {content}")
                except Exception as e:
                    if not max_uid:  # To avoid mess in the debug logs
                        if "RPC_AUTH_ERROR: AUTH_REJECTEDCRED" in str(e):
                            self.logger.fail(f"{share} - RPC Access denied")
                        elif "RPC_AUTH_ERROR: AUTH_TOOWEAK" in str(e):
                            self.logger.fail(f"{share} - Kerberos authentication required")
                        elif "Insufficient Permissions" in str(e):
                            self.logger.fail(f"{share} - Insufficient Permissions for share listing")
                        else:
                            self.logger.exception(f"{share} - {e}")


def convert_size(size_bytes):
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 1)
    return f"{s}{size_name[i]}"
