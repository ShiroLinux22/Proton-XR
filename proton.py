#!/usr/bin/env python3

#script to launch Wine with the correct environment

import fcntl
import array
import filecmp
import fnmatch
import json
import os
import shutil
import errno
import stat
import subprocess
import sys
import tarfile

from ctypes import CDLL
from ctypes import POINTER
from ctypes import Structure
from ctypes import addressof
from ctypes import cast
from ctypes import c_int
from ctypes import c_char_p
from ctypes import c_void_p

from filelock import FileLock
from random import randrange

#To enable debug logging, copy "user_settings.sample.py" to "user_settings.py"
#and edit it if needed.

CURRENT_PREFIX_VERSION="6.20-GE-1"

PFX="Proton: "
ld_path_var = "LD_LIBRARY_PATH"

def nonzero(s):
    return len(s) > 0 and s != "0"

def prepend_to_env_str(env, variable, prepend_str, separator):
    if not variable in env:
        env[variable] = prepend_str
    else:
        env[variable] = prepend_str + separator + env[variable]

def append_to_env_str(env, variable, append_str, separator):
    if not variable in env:
        env[variable] = append_str
    else:
        env[variable] = env[variable] + separator + append_str

def log(msg):
    sys.stderr.write(PFX + msg + os.linesep)
    sys.stderr.flush()

def file_is_wine_builtin_dll(path):
    if os.path.islink(path):
        contents = os.readlink(path)
        if os.path.dirname(contents).endswith(('/lib/wine/i386-windows', '/lib64/wine/x86_64-windows')):
            # This may be a broken link to a dll in a removed Proton install
            return True
    if not os.path.exists(path):
        return False
    try:
        sfile = open(path, "rb")
        sfile.seek(0x40)
        tag = sfile.read(20)
        return tag.startswith((b"Wine placeholder DLL", b"Wine builtin DLL"))
    except IOError:
        return False

def makedirs(path):
    try:
        os.makedirs(path)
    except OSError:
        #already exists
        pass

def merge_user_dir(src, dst):
    extant_dirs = []
    for src_dir, dirs, files in os.walk(src):
        dst_dir = src_dir.replace(src, dst, 1)

        #as described below, avoid merging game save subdirs, too
        child_of_extant_dir = False
        for dir_ in extant_dirs:
            if dir_ in dst_dir:
                child_of_extant_dir = True
                break
        if child_of_extant_dir:
            continue

        #we only want to copy into directories which don't already exist. games
        #may not react well to two save directory instances being merged.
        if not os.path.exists(dst_dir) or os.path.samefile(dst_dir, dst):
            makedirs(dst_dir)
            for dir_ in dirs:
                src_file = os.path.join(src_dir, dir_)
                dst_file = os.path.join(dst_dir, dir_)
                if os.path.islink(src_file) and not os.path.exists(dst_file):
                    try_copy(src_file, dst_file, copy_metadata=True, follow_symlinks=False)
            for file_ in files:
                src_file = os.path.join(src_dir, file_)
                dst_file = os.path.join(dst_dir, file_)
                if not os.path.exists(dst_file):
                    try_copy(src_file, dst_file, copy_metadata=True, follow_symlinks=False)
        else:
            extant_dirs += dst_dir

def try_copy(src, dst, add_write_perm=True, copy_metadata=False, optional=False, follow_symlinks=True):
    try:
        if os.path.isdir(dst):
            dstfile = dst + "/" + os.path.basename(src)
            if os.path.lexists(dstfile):
                os.remove(dstfile)
        else:
            dstfile = dst
            if os.path.lexists(dst):
                os.remove(dst)

        if copy_metadata:
            shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
        else:
            shutil.copy(src, dst, follow_symlinks=follow_symlinks)

        if add_write_perm:
            new_mode = os.lstat(dstfile).st_mode | stat.S_IWUSR | stat.S_IWGRP
            os.chmod(dstfile, new_mode)

    except FileNotFoundError as e:
        if optional:
            log('Error while copying to \"' + dst + '\": ' + e.strerror)
        else:
            raise

    except PermissionError as e:
        if e.errno == errno.EPERM:
            #be forgiving about permissions errors; if it's a real problem, things will explode later anyway
            log('Error while copying to \"' + dst + '\": ' + e.strerror)
        else:
            raise

def try_copyfile(src, dst):
    try:
        if os.path.isdir(dst):
            dstfile = dst + "/" + os.path.basename(src)
            if os.path.lexists(dstfile):
                os.remove(dstfile)
        elif os.path.lexists(dst):
            os.remove(dst)
        shutil.copyfile(src, dst)
    except PermissionError as e:
        if e.errno == errno.EPERM:
            #be forgiving about permissions errors; if it's a real problem, things will explode later anyway
            log('Error while copying to \"' + dst + '\": ' + e.strerror)
        else:
            raise

def getmtimestr(*path_fragments):
    path = os.path.join(*path_fragments)
    try:
        return str(os.path.getmtime(path))
    except IOError:
        return "0"

def try_get_game_library_dir():
    if not "STEAM_COMPAT_INSTALL_PATH" in g_session.env or \
            not "STEAM_COMPAT_LIBRARY_PATHS" in g_session.env:
        return None

    #find library path which is a subset of the game path
    library_paths = g_session.env["STEAM_COMPAT_LIBRARY_PATHS"].split(":")
    for l in library_paths:
        if l in g_session.env["STEAM_COMPAT_INSTALL_PATH"]:
            return l

    return None

# Function to find the installed location of DLL files for use by Wine/Proton
# from the NVIDIA Linux driver
#
# See https://gitlab.steamos.cloud/steamrt/steam-runtime-tools/-/issues/71 for
# background on the chosen method of DLL discovery.
#
# On success, returns a str() of the absolute-path to the directory at which DLL
# files are stored
#
# On failure, returns None
def find_nvidia_wine_dll_dir():
    try:
        libdl = CDLL("libdl.so.2")
    except (OSError):
        return None

    try:
        libglx_nvidia = CDLL("libGLX_nvidia.so.0")
    except OSError:
        return None

    # from dlinfo(3)
    #
    # struct link_map {
    #     ElfW(Addr) l_addr;  /* Difference between the
    #                            address in the ELF file and
    #                            the address in memory */
    #     char      *l_name;  /* Absolute pathname where
    #                            object was found */
    #     ElfW(Dyn) *l_ld;    /* Dynamic section of the
    #                            shared object */
    #     struct link_map *l_next, *l_prev;
    #                         /* Chain of loaded objects */
    #
    #     /* Plus additional fields private to the
    #        implementation */
    # };
    RTLD_DI_LINKMAP = 2
    class link_map(Structure):
        _fields_ = [("l_addr", c_void_p), ("l_name", c_char_p), ("l_ld", c_void_p)]

    # from dlinfo(3)
    #
    # int dlinfo (void *restrict handle, int request, void *restrict info)
    dlinfo_func = libdl.dlinfo
    dlinfo_func.argtypes = c_void_p, c_int, c_void_p
    dlinfo_func.restype = c_int

    # Allocate a link_map object
    glx_nvidia_info_ptr = POINTER(link_map)()

    # Run dlinfo(3) on the handle to libGLX_nvidia.so.0, storing results at the
    # address represented by glx_nvidia_info_ptr
    if dlinfo_func(libglx_nvidia._handle,
                   RTLD_DI_LINKMAP,
                   addressof(glx_nvidia_info_ptr)) != 0:
        return None

    # Grab the contents our of our pointer
    glx_nvidia_info = cast(glx_nvidia_info_ptr, POINTER(link_map)).contents

    # Decode the path to our library to a str()
    if glx_nvidia_info.l_name is None:
        return None
    try:
        libglx_nvidia_path = os.fsdecode(glx_nvidia_info.l_name)
    except UnicodeDecodeError:
        return None

    # Follow any symlinks to the actual file
    libglx_nvidia_realpath = os.path.realpath(libglx_nvidia_path)

    # Go to the relative path ./nvidia/wine from our library
    nvidia_wine_dir = os.path.join(os.path.dirname(libglx_nvidia_realpath), "nvidia", "wine")

    # Check that nvngx.dll exists here, or fail
    if os.path.exists(os.path.join(nvidia_wine_dir, "nvngx.dll")):
        return nvidia_wine_dir

    return None

EXT2_IOC_GETFLAGS = 0x80086601
EXT2_IOC_SETFLAGS = 0x40086602

EXT4_CASEFOLD_FL = 0x40000000

def set_dir_casefold_bit(dir_path):
    dr = os.open(dir_path, 0o644)
    if dr < 0:
        return
    try:
        dat = array.array('I', [0])
        if fcntl.ioctl(dr, EXT2_IOC_GETFLAGS, dat, True) >= 0:
            dat[0] = dat[0] | EXT4_CASEFOLD_FL
            fcntl.ioctl(dr, EXT2_IOC_SETFLAGS, dat, False)
    except (OSError, IOError):
        #no problem
        pass
    os.close(dr)

class Proton:
    def __init__(self, base_dir):
        self.base_dir = base_dir + "/"
        self.dist_dir = self.path("files/")
        self.bin_dir = self.path("files/bin/")
        self.lib_dir = self.path("files/lib/")
        self.lib64_dir = self.path("files/lib64/")
        self.fonts_dir = self.path("files/share/fonts/")
        self.wine_fonts_dir = self.path("files/share/wine/fonts/")
        self.version_file = self.path("version")
        self.default_pfx_dir = self.path("files/share/default_pfx/")
        self.user_settings_file = self.path("user_settings.py")
        self.wine_bin = self.bin_dir + "wine"
        self.wine64_bin = self.bin_dir + "wine64"
        self.wineserver_bin = self.bin_dir + "wineserver"
        self.dist_lock = FileLock(self.path("dist.lock"), timeout=-1)

    def path(self, d):
        return self.base_dir + d

    def cleanup_legacy_dist(self):
        old_dist_dir = self.path("dist/")
        if os.path.exists(old_dist_dir):
            with self.dist_lock:
                if os.path.exists(old_dist_dir):
                    shutil.rmtree(old_dist_dir)

    def do_steampipe_fixups(self):
        fixups_json = self.path("steampipe_fixups.json")
        fixups_mtime = self.path("files/steampipe_fixups_mtime")

        if os.path.exists(fixups_json):
            with self.dist_lock:
                import steampipe_fixups

                current_fixup_mtime = None
                if os.path.exists(fixups_mtime):
                    with open(fixups_mtime, "r") as f:
                        current_fixup_mtime = f.readline().strip()

                new_fixup_mtime = getmtimestr(fixups_json)

                if current_fixup_mtime != new_fixup_mtime:
                    result_code = steampipe_fixups.do_restore(self.base_dir, fixups_json)

                    if result_code == 0:
                        with open(fixups_mtime, "w") as f:
                            f.write(new_fixup_mtime + "\n")

    def missing_default_prefix(self):
        '''Check if the default prefix dir is missing. Returns true if missing, false if present'''
        return not os.path.isdir(self.default_pfx_dir)

    def make_default_prefix(self):
        with self.dist_lock:
            local_env = dict(g_session.env)
            if self.missing_default_prefix():
                #make default prefix
                local_env["WINEPREFIX"] = self.default_pfx_dir
                local_env["WINEDEBUG"] = "-all"
                g_session.run_proc([self.wine64_bin, "wineboot"], local_env)
                g_session.run_proc([self.wineserver_bin, "-w"], local_env)

class CompatData:
    def __init__(self, compatdata):
        self.base_dir = compatdata + "/"
        self.prefix_dir = self.path("pfx/")
        self.version_file = self.path("version")
        self.config_info_file = self.path("config_info")
        self.tracked_files_file = self.path("tracked_files")
        self.prefix_lock = FileLock(self.path("pfx.lock"), timeout=-1)

    def path(self, d):
        return self.base_dir + d

    def remove_tracked_files(self):
        if not os.path.exists(self.tracked_files_file):
            log("Prefix has no tracked_files??")
            return

        with open(self.tracked_files_file, "r") as tracked_files:
            dirs = []
            for f in tracked_files:
                path = self.prefix_dir + f.strip()
                if os.path.exists(path):
                    if os.path.isfile(path) or os.path.islink(path):
                        os.remove(path)
                    else:
                        dirs.append(path)
            for d in dirs:
                try:
                    os.rmdir(d)
                except OSError:
                    #not empty
                    pass

        os.remove(self.tracked_files_file)
        os.remove(self.version_file)

    def upgrade_pfx(self, old_ver):
        if old_ver == CURRENT_PREFIX_VERSION:
            return

        log("Upgrading prefix from " + str(old_ver) + " to " + CURRENT_PREFIX_VERSION + " (" + self.base_dir + ")")

        if old_ver is None:
            return

        if not '-' in old_ver:
            #How can this happen??
            log("Prefix has an invalid version?! You may want to back up user files and delete this prefix.")
            #If it does, just let the Wine upgrade happen and hope it works...
            return

        try:
            old_proton_ver, old_prefix_ver = old_ver.split('-')
            old_proton_maj, old_proton_min = old_proton_ver.split('.')
            new_proton_ver, new_prefix_ver = CURRENT_PREFIX_VERSION.split('-')
            new_proton_maj, new_proton_min = new_proton_ver.split('.')

            if int(new_proton_maj) < int(old_proton_maj) or \
                    (int(new_proton_maj) == int(old_proton_maj) and \
                     int(new_proton_min) < int(old_proton_min)):
                log("Removing newer prefix")
                if old_proton_ver == "3.7" and not os.path.exists(self.tracked_files_file):
                    #proton 3.7 did not generate tracked_files, so copy it into place first
                    try_copy(g_proton.path("proton_3.7_tracked_files"), self.tracked_files_file)
                self.remove_tracked_files()
                return

            if old_proton_ver == "3.7" and old_prefix_ver == "1":
                if not os.path.exists(self.prefix_dir + "/drive_c/windows/syswow64/kernel32.dll"):
                    #shipped a busted 64-bit-only installation on 20180822. detect and wipe clean
                    log("Detected broken 64-bit-only installation, re-creating prefix.")
                    shutil.rmtree(self.prefix_dir)
                    return

            #replace broken .NET installations with wine-mono support
            if os.path.exists(self.prefix_dir + "/drive_c/windows/Microsoft.NET/NETFXRepair.exe") and \
                    file_is_wine_builtin_dll(self.prefix_dir + "/drive_c/windows/system32/mscoree.dll"):
                log("Broken .NET installation detected, switching to wine-mono.")
                #deleting this directory allows wine-mono to work
                shutil.rmtree(self.prefix_dir + "/drive_c/windows/Microsoft.NET")

            #prior to prefix version 4.11-2, all controllers were xbox controllers. wipe out the old registry entries.
            if (int(old_proton_maj) < 4 or (int(old_proton_maj) == 4 and int(old_proton_min) == 11)) and \
                    int(old_prefix_ver) < 2:
                log("Removing old xinput registry entries.")
                with open(self.prefix_dir + "system.reg", "r") as reg_in:
                    with open(self.prefix_dir + "system.reg.new", "w") as reg_out:
                        for line in reg_in:
                            if line[0] == '[' and "CurrentControlSet" in line and "IG_" in line:
                                if "DeviceClasses" in line:
                                    reg_out.write(line.replace("DeviceClasses", "DeviceClasses_old"))
                                elif "Enum" in line:
                                    reg_out.write(line.replace("Enum", "Enum_old"))
                            else:
                                reg_out.write(line)
                try:
                    os.rename(self.prefix_dir + "system.reg", self.prefix_dir + "system.reg.old")
                except OSError:
                    os.remove(self.prefix_dir + "system.reg")
                    pass

                try:
                    os.rename(self.prefix_dir + "system.reg.new", self.prefix_dir + "system.reg")
                except OSError:
                    log("Unable to write new registry file to " + self.prefix_dir + "system.reg")
                    pass

            # Prior to prefix version 6.3-3, ShellExecute* APIs used DDE.
            # Wipe out old registry entries.
            if int(old_proton_maj) < 6 or (int(old_proton_maj) == 6 and int(old_proton_min) < 3) or \
                    (int(old_proton_maj) == 6 and int(old_proton_min) == 3 and int(old_prefix_ver) < 3):
                delete_keys = {
                    "[Software\\\\Classes\\\\htmlfile\\\\shell\\\\open\\\\ddeexec",
                    "[Software\\\\Classes\\\\pdffile\\\\shell\\\\open\\\\ddeexec",
                    "[Software\\\\Classes\\\\xmlfile\\\\shell\\\\open\\\\ddeexec",
                    "[Software\\\\Classes\\\\ftp\\\\shell\\\\open\\\\ddeexec",
                    "[Software\\\\Classes\\\\http\\\\shell\\\\open\\\\ddeexec",
                    "[Software\\\\Classes\\\\https\\\\shell\\\\open\\\\ddeexec",
                }
                dde_wb = '@="\\"C:\\\\windows\\\\system32\\\\winebrowser.exe\\" -nohome"'

                sysreg_fp = self.prefix_dir + "system.reg"
                new_sysreg_fp = self.prefix_dir + "system.reg.new"

                log("Removing ShellExecute DDE registry entries.")

                with open(sysreg_fp, "r") as reg_in:
                    with open(new_sysreg_fp, "w") as reg_out:
                        for line in reg_in:
                            if line[:line.find("ddeexec")+len("ddeexec")] in delete_keys:
                                reg_out.write(line.replace("ddeexec", "ddeexec_old", 1))
                            elif line.rstrip() == dde_wb:
                                reg_out.write(line.replace("-nohome", "%1"))
                            else:
                                reg_out.write(line)

                # Slightly randomize backup file name to avoid colliding with
                # other backups.
                backup_sysreg_fp = "{}system.reg.{:x}.old".format(self.prefix_dir, randrange(16 ** 8))

                try:
                    os.rename(sysreg_fp, backup_sysreg_fp)
                except OSError:
                    log("Failed to back up old system.reg. Simply deleting it.")
                    os.remove(sysreg_fp)
                    pass

                try:
                    os.rename(new_sysreg_fp, sysreg_fp)
                except OSError:
                    log("Unable to write new registry file to " + sysreg_fp)
                    pass

            stale_builtins = [self.prefix_dir + "/drive_c/windows/system32/amd_ags_x64.dll",
                              self.prefix_dir + "/drive_c/windows/syswow64/amd_ags_x64.dll" ]
            for builtin in stale_builtins:
                if os.path.lexists(builtin) and file_is_wine_builtin_dll(builtin):
                    log("Removing stale builtin " + builtin)
                    os.remove(builtin)

        except ValueError:
            log("Prefix has an invalid version?! You may want to back up user files and delete this prefix.")
            #Just let the Wine upgrade happen and hope it works...
            return

    def pfx_copy(self, src, dst, dll_copy=False):
        if os.path.islink(src):
            contents = os.readlink(src)
            if os.path.dirname(contents).endswith(('/lib/wine/i386-windows', '/lib64/wine/x86_64-windows')):
                # wine builtin dll
                # make the destination an absolute symlink
                contents = os.path.normpath(os.path.join(os.path.dirname(src), contents))
            if dll_copy:
                try_copyfile(src, dst)
            else:
                os.symlink(contents, dst)
        else:
            try_copyfile(src, dst)

    def copy_pfx(self):
        with open(self.tracked_files_file, "w") as tracked_files:
            for src_dir, dirs, files in os.walk(g_proton.default_pfx_dir):
                rel_dir = src_dir.replace(g_proton.default_pfx_dir, "", 1).lstrip('/')
                if len(rel_dir) > 0:
                    rel_dir = rel_dir + "/"
                dst_dir = src_dir.replace(g_proton.default_pfx_dir, self.prefix_dir, 1)
                if not os.path.lexists(dst_dir):
                    os.makedirs(dst_dir)
                    tracked_files.write(rel_dir + "\n")
                for dir_ in dirs:
                    src_file = os.path.join(src_dir, dir_)
                    dst_file = os.path.join(dst_dir, dir_)
                    if os.path.islink(src_file) and not os.path.exists(dst_file):
                        self.pfx_copy(src_file, dst_file)
                for file_ in files:
                    src_file = os.path.join(src_dir, file_)
                    dst_file = os.path.join(dst_dir, file_)
                    if not os.path.exists(dst_file):
                        self.pfx_copy(src_file, dst_file)
                        tracked_files.write(rel_dir + file_ + "\n")

    def update_builtin_libs(self, dll_copy_patterns):
        dll_copy_patterns = dll_copy_patterns.split(',')
        prev_tracked_files = set()
        with open(self.tracked_files_file, "r") as tracked_files:
            for line in tracked_files:
                prev_tracked_files.add(line.strip())
        with open(self.tracked_files_file, "a") as tracked_files:
            for src_dir, dirs, files in os.walk(g_proton.default_pfx_dir):
                rel_dir = src_dir.replace(g_proton.default_pfx_dir, "", 1).lstrip('/')
                if len(rel_dir) > 0:
                    rel_dir = rel_dir + "/"
                dst_dir = src_dir.replace(g_proton.default_pfx_dir, self.prefix_dir, 1)
                if not os.path.lexists(dst_dir):
                    os.makedirs(dst_dir)
                    tracked_files.write(rel_dir + "\n")
                for file_ in files:
                    src_file = os.path.join(src_dir, file_)
                    dst_file = os.path.join(dst_dir, file_)
                    if not file_is_wine_builtin_dll(src_file):
                        # Not a builtin library
                        continue
                    if file_is_wine_builtin_dll(dst_file):
                        os.unlink(dst_file)
                    elif os.path.lexists(dst_file):
                        # builtin library was replaced
                        continue
                    else:
                        os.makedirs(dst_dir, exist_ok=True)
                    dll_copy = any(fnmatch.fnmatch(file_, pattern) for pattern in dll_copy_patterns)
                    self.pfx_copy(src_file, dst_file, dll_copy)
                    tracked_name = rel_dir + file_
                    if tracked_name not in prev_tracked_files:
                        tracked_files.write(tracked_name + "\n")

    def create_fonts_symlinks(self):
        fontsmap = [
            ( g_proton.fonts_dir, "LiberationSans-Regular.ttf", "arial.ttf" ),
            ( g_proton.fonts_dir, "LiberationSans-Bold.ttf", "arialbd.ttf" ),
            ( g_proton.fonts_dir, "LiberationSerif-Regular.ttf", "times.ttf" ),
            ( g_proton.fonts_dir, "LiberationMono-Regular.ttf", "cour.ttf" ),
            ( g_proton.fonts_dir, "LiberationMono-Bold.ttf", "courbd.ttf" ),
            ( g_proton.fonts_dir, "msyh.ttf", "msyh.ttf" ),
            ( g_proton.fonts_dir, "simsun.ttc", "simsun.ttc" ),
            ( g_proton.fonts_dir, "msgothic.ttc", "msgothic.ttc" ),
            ( g_proton.fonts_dir, "malgun.ttf", "malgun.ttf" ),
            ( g_proton.fonts_dir, "NotoSansArabic-Regular.ttf", "NotoSansArabic-Regular.ttf" ),

            ( g_proton.wine_fonts_dir, "tahoma.ttf", "tahoma.ttf" ),
        ]

        windowsfonts = self.prefix_dir + "/drive_c/windows/Fonts"
        makedirs(windowsfonts)
        for p in fontsmap:
            lname = os.path.join(windowsfonts, p[2])
            fname = os.path.join(p[0], p[1])
            if os.path.lexists(lname):
                if os.path.islink(lname):
                    os.remove(lname)
                    os.symlink(fname, lname)
            else:
                os.symlink(fname, lname)

    def migrate_user_paths(self):
        #move winxp-style paths to vista+ paths. we can't do this in
        #upgrade_pfx because Steam may drop cloud files here at any time.
        for (old, new, link) in \
                [
                    ("drive_c/users/steamuser/Local Settings/Application Data",
                        self.prefix_dir + "drive_c/users/steamuser/AppData/Local",
                        "../AppData/Local"),
                    ("drive_c/users/steamuser/Application Data",
                        self.prefix_dir + "drive_c/users/steamuser/AppData/Roaming",
                        "./AppData/Roaming"),
                    ("drive_c/users/steamuser/My Documents",
                        self.prefix_dir + "drive_c/users/steamuser/Documents",
                        "./Documents"),
                ]:

            #running unofficial Proton/Wine builds against a Proton prefix could
            #create an infinite symlink loop. detect this and clean it up.
            if os.path.lexists(new) and os.path.islink(new) and os.readlink(new).endswith(old):
                os.remove(new)

            old = self.prefix_dir + old

            if os.path.lexists(old) and not os.path.islink(old):
                merge_user_dir(src=old, dst=new)
                os.rename(old, old + " BACKUP")
            if not os.path.lexists(old):
                makedirs(os.path.dirname(old))
                os.symlink(src=link, dst=old)
            elif os.path.islink(old) and not (os.readlink(old) == link):
                os.remove(old)
                os.symlink(src=link, dst=old)

    def setup_prefix(self):
        with self.prefix_lock:
            if os.path.exists(self.version_file):
                with open(self.version_file, "r") as f:
                    old_ver = f.readline().strip()
            else:
                old_ver = None

            self.upgrade_pfx(old_ver)

            if not os.path.exists(self.prefix_dir):
                makedirs(self.prefix_dir + "/drive_c")
                set_dir_casefold_bit(self.prefix_dir + "/drive_c")
                if not os.path.exists(self.prefix_dir + "/dosdevices"):
                    makedirs(self.prefix_dir + "/dosdevices")
                    set_dir_casefold_bit(self.prefix_dir + "/dosdevices")

            if not os.path.exists(self.prefix_dir + "/user.reg"):
                self.copy_pfx()

            self.migrate_user_paths()

            if not os.path.lexists(self.prefix_dir + "/dosdevices/c:"):
                os.symlink("../drive_c", self.prefix_dir + "/dosdevices/c:")

            if not os.path.lexists(self.prefix_dir + "/dosdevices/z:"):
                os.symlink("/", self.prefix_dir + "/dosdevices/z:")

            # collect configuration info
            steamdir = os.environ["STEAM_COMPAT_CLIENT_INSTALL_PATH"]

            use_wined3d = "wined3d" in g_session.compat_config
            use_dxvk_dxgi = not use_wined3d and \
                    not ("WINEDLLOVERRIDES" in g_session.env and "dxgi=b" in g_session.env["WINEDLLOVERRIDES"])
            use_nvapi = 'enablenvapi' in g_session.compat_config

            builtin_dll_copy = os.environ.get("PROTON_DLL_COPY",
                    #dxsetup redist
                    "d3dcompiler_*.dll," +
                    "d3dcsx*.dll," +
                    "d3dx*.dll," +
                    "x3daudio*.dll," +
                    "xactengine*.dll," +
                    "xapofx*.dll," +
                    "xaudio*.dll," +
                    "xinput*.dll," +
                    "devenum.dll," +

                    #directshow
                    "amstream.dll," +
                    "qasf.dll," +
                    "qcap.dll," +
                    "qdvd.dll," +
                    "qedit.dll," +
                    "quartz.dll," +

                    #directplay
                    "dplay.dll," +
                    "dplaysvr.exe," +
                    "dplayx.dll," +
                    "dpmodemx.dll," +
                    "dpnaddr.dll," +
                    "dpnet.dll," +
                    "dpnlobby.dll," +
                    "dpnhpast.dll," +
                    "dpnhupnp.dll," +
                    "dpnsvr.exe," +
                    "dpwsockx.dll," +
                    "dpvoice.dll," +

                    #directmusic
                    "dmband.dll," +
                    "dmcompos.dll," +
                    "dmime.dll," +
                    "dmloader.dll," +
                    "dmscript.dll," +
                    "dmstyle.dll," +
                    "dmsynth.dll," +
                    "dmusic.dll," +
                    "dmusic32.dll," +
                    "dsound.dll," +
                    "dswave.dll," +

                    #vcruntime redist
                    "atl1*.dll," +
                    "concrt1*.dll," +
                    "msvcp1*.dll," +
                    "msvcr1*.dll," +
                    "vcamp1*.dll," +
                    "vcomp1*.dll," +
                    "vccorlib1*.dll," +
                    "vcruntime1*.dll," +
                    "api-ms-win-crt-conio-l1-1-0.dll," +
                    "api-ms-win-crt-heap-l1-1-0.dll," +
                    "api-ms-win-crt-locale-l1-1-0.dll," +
                    "api-ms-win-crt-math-l1-1-0.dll," +
                    "api-ms-win-crt-runtime-l1-1-0.dll," +
                    "api-ms-win-crt-stdio-l1-1-0.dll," +
                    "ucrtbase.dll," +

                    #some games balk at ntdll symlink(?)
                    "ntdll.dll," +

                    #some games require official vulkan loader
                    "vulkan-1.dll," +

                    #wmp9
                    "dispex.dll," +
                    "jscript.dll," +
                    "scrobj.dll," +
                    "scrrun.dll," +
                    "vbscript.dll," +
                    "cscript.exe," +
                    "wscript.exe," +
                    "wshom.ocx," +
                    "9SeriesDefault.wmz," +
                    "9SeriesDefault_.wmz," +
                    "9xmigrat.dll," +
                    "advpack.dll," +
                    "asferror.dll," +
                    "blackbox.dll," +
                    "CEWMDM.dll," +
                    "Compact.wmz," +
                    "control.xml," +
                    "custsat.dll," +
                    "drm.cat," +
                    "drm.inf," +
                    "DRMClien.dll," +
                    "DrmStor.dll," +
                    "drmv2clt.dll," +
                    "dw15.exe," +
                    "dwintl.dll," +
                    "engsetup.exe," +
                    "eula.txt," +
                    "fhg.inf," +
                    "iexpress.inf," +
                    "l3codeca.acm," +
                    "LAPRXY.DLL," +
                    "logagent.exe," +
                    "migrate.dll," +
                    "migrate.exe," +
                    "MP43DMOD.DLL," +
                    "MP4SDMOD.DLL," +
                    "MPG4DMOD.DLL," +
                    "mpvis.DLL," +
                    "msdmo.dll," +
                    "msnetobj.dll," +
                    "msoobci.dll," +
                    "MsPMSNSv.dll," +
                    "MsPMSP.dll," +
                    "MSSCP.dll," +
                    "MSWMDM.dll," +
                    "mymusic.inf," +
                    "npdrmv2.dll," +
                    "npdrmv2.zip," +
                    "NPWMSDrm.dll," +
                    "PidGen.dll," +
                    "Plylst1.wpl," +
                    "Plylst10.wpl," +
                    "Plylst11.wpl," +
                    "Plylst12.wpl," +
                    "Plylst13.wpl," +
                    "Plylst14.wpl," +
                    "Plylst15.wpl," +
                    "Plylst2.wpl," +
                    "Plylst3.wpl," +
                    "Plylst4.wpl," +
                    "Plylst5.wpl," +
                    "Plylst6.wpl," +
                    "Plylst7.wpl," +
                    "Plylst8.wpl," +
                    "Plylst9.wpl," +
                    "plyr_err.chm," +
                    "qasf.dll," +
                    "QuickSilver.wmz," +
                    "Revert.wmz," +
                    "roxio.inf," +
                    "rsl.dll," +
                    "setup_wm.cat," +
                    "setup_wm.exe," +
                    "setup_wm.inf," +
                    "skins.inf," +
                    "skinsmui.inf," +
                    "unicows.dll," +
                    "unregmp2.exe," +
                    "w95inf16.dll," +
                    "w95inf32.dll," +
                    "wm1033.lng," +
                    "WMADMOD.DLL," +
                    "WMADMOE.DLL," +
                    "WMASF.DLL," +
                    "wmburn.exe," +
                    "wmburn.rxc," +
                    "wmdm.cat," +
                    "wmdm.inf," +
                    "WMDMLOG.dll," +
                    "WMDMPS.dll," +
                    "wmerror.dll," +
                    "wmexpack.cat," +
                    "wmexpack.inf," +
                    "WMFSDK.cat," +
                    "WMFSDK.inf," +
                    "wmidx.dll," +
                    "WMNetMgr.dll," +
                    "wmp.cat," +
                    "wmp.dll," +
                    "wmp.inf," +
                    "wmp.ocx," +
                    "wmpasf.dll," +
                    "wmpband.dll," +
                    "wmpcd.dll," +
                    "wmpcore.dll," +
                    "wmpdxm.dll," +
                    "wmplayer.adm," +
                    "wmplayer.chm," +
                    "wmplayer.exe," +
                    "wmploc.DLL," +
                    "WMPNS.dll," +
                    "wmpns.jar," +
                    "wmpshell.dll," +
                    "wmpui.dll," +
                    "WMSDMOD.DLL," +
                    "WMSDMOE2.DLL," +
                    "WMSPDMOD.DLL," +
                    "WMSPDMOE.DLL," +
                    "WMVCORE.DLL," +
                    "WMVDMOD.DLL," +
                    "WMVDMOE2.DLL"
                    )

            # If any of this info changes, we must rerun the tasks below
            prefix_info = '\n'.join((
                CURRENT_PREFIX_VERSION,
                g_proton.fonts_dir,
                g_proton.lib_dir,
                g_proton.lib64_dir,
                steamdir,
                getmtimestr(steamdir, 'legacycompat', 'steamclient.dll'),
                getmtimestr(steamdir, 'legacycompat', 'steamclient64.dll'),
                getmtimestr(steamdir, 'legacycompat', 'Steam.dll'),
                g_proton.default_pfx_dir,
                getmtimestr(g_proton.default_pfx_dir, 'system.reg'),
                str(use_wined3d),
                str(use_dxvk_dxgi),
                builtin_dll_copy,
                str(use_nvapi),
            ))

            # check whether any prefix config has changed
            try:
                with open(self.config_info_file, "r") as f:
                    old_prefix_info = f.read()
            except IOError:
                old_prefix_info = ""

            if old_ver != CURRENT_PREFIX_VERSION or old_prefix_info != prefix_info:
                # update builtin dll symlinks or copies
                self.update_builtin_libs(builtin_dll_copy)

                with open(self.config_info_file, "w") as f:
                    f.write(prefix_info)

            with open(self.version_file, "w") as f:
                f.write(CURRENT_PREFIX_VERSION + "\n")

            #create font files symlinks
            self.create_fonts_symlinks()

            with open(self.tracked_files_file, "a") as tracked_files:
                #copy steam files into place
                steam_dir = "drive_c/Program Files (x86)/Steam/"
                dst = self.prefix_dir + steam_dir
                makedirs(dst)
                filestocopy = [("steamclient.dll", "steamclient.dll"),
                               ("steamclient64.dll", "steamclient64.dll"),
                               ("GameOverlayRenderer64.dll", "GameOverlayRenderer64.dll"),
                               ("SteamService.exe", "steam.exe"),
                               ("Steam.dll", "Steam.dll")]
                for (src,tgt) in filestocopy:
                    srcfile = steamdir + '/legacycompat/' + src
                    if os.path.isfile(srcfile):
                        dstfile = dst + tgt
                        if os.path.isfile(dstfile):
                            os.remove(dstfile)
                        else:
                            tracked_files.write(steam_dir + tgt + "\n")
                        try_copy(srcfile, dstfile)

                filestocopy = [("steamclient64.dll", "steamclient64.dll"),
                               ("GameOverlayRenderer.dll", "GameOverlayRenderer.dll"),
                               ("GameOverlayRenderer64.dll", "GameOverlayRenderer64.dll")]
                for (src,tgt) in filestocopy:
                    srcfile = g_proton.path(src)
                    if os.path.isfile(srcfile):
                        dstfile = dst + tgt
                        if os.path.isfile(dstfile):
                            os.remove(dstfile)
                        else:
                            tracked_files.write(steam_dir + tgt + "\n")
                        try_copy(srcfile, dstfile)

            #copy openvr files into place
            dst = self.prefix_dir + "/drive_c/vrclient/bin/"
            makedirs(dst)
            try_copy(g_proton.lib_dir + "wine/i386-windows/vrclient.dll", dst)
            try_copy(g_proton.lib64_dir + "wine/x86_64-windows/vrclient_x64.dll", dst)

            try_copy(g_proton.lib_dir + "wine/dxvk/openvr_api_dxvk.dll", self.prefix_dir + "/drive_c/windows/syswow64/")
            try_copy(g_proton.lib64_dir + "wine/dxvk/openvr_api_dxvk.dll", self.prefix_dir + "/drive_c/windows/system32/")

            makedirs(self.prefix_dir + "/drive_c/openxr/")
            try_copy(g_proton.default_pfx_dir + "drive_c/openxr/wineopenxr64.json", self.prefix_dir + "/drive_c/openxr/")

            #copy vkd3d files into place
            try_copy(g_proton.lib64_dir + "vkd3d/libvkd3d-shader-1.dll",
                    self.prefix_dir + "drive_c/windows/system32/libvkd3d-shader-1.dll")
            try_copy(g_proton.lib_dir + "vkd3d/libvkd3d-shader-1.dll",
                    self.prefix_dir + "drive_c/windows/syswow64/libvkd3d-shader-1.dll")

            if use_wined3d:
                dxvkfiles = ["dxvk_config"]
                wined3dfiles = ["d3d11", "d3d10", "d3d10core", "d3d10_1", "d3d9"]
            else:
                dxvkfiles = ["dxvk_config", "d3d11", "d3d10", "d3d10core", "d3d10_1", "d3d9"]
                wined3dfiles = []

            if use_dxvk_dxgi:
                dxvkfiles.append("dxgi")
            else:
                wined3dfiles.append("dxgi")

            for f in wined3dfiles:
                try_copy(g_proton.default_pfx_dir + "drive_c/windows/system32/" + f + ".dll",
                        self.prefix_dir + "drive_c/windows/system32/" + f + ".dll")
                try_copy(g_proton.default_pfx_dir + "drive_c/windows/syswow64/" + f + ".dll",
                        self.prefix_dir + "drive_c/windows/syswow64/" + f + ".dll")

            for f in dxvkfiles:
                try_copy(g_proton.lib64_dir + "wine/dxvk/" + f + ".dll",
                        self.prefix_dir + "drive_c/windows/system32/" + f + ".dll")
                try_copy(g_proton.lib_dir + "wine/dxvk/" + f + ".dll",
                        self.prefix_dir + "drive_c/windows/syswow64/" + f + ".dll")
                g_session.dlloverrides[f] = "n"

            # If the user requested the NVAPI be available, copy it into place.
            # If they didn't, clean up any stray nvapi DLLs.
            if use_nvapi:
                try_copy(g_proton.lib64_dir + "wine/nvapi/nvapi64.dll",
                        self.prefix_dir + "drive_c/windows/system32/nvapi64.dll")
                try_copy(g_proton.lib_dir + "wine/nvapi/nvapi.dll",
                        self.prefix_dir + "drive_c/windows/syswow64/nvapi.dll")
                g_session.dlloverrides["nvapi64"] = "n"
                g_session.dlloverrides["nvapi"] = "n"
                g_session.dlloverrides["nvcuda"] = "b"
            else:
                nvapi64_dll = self.prefix_dir + "drive_c/windows/system32/nvapi64.dll"
                nvapi32_dll = self.prefix_dir + "drive_c/windows/syswow64/nvapi.dll"
                if os.path.exists(nvapi64_dll):
                    os.unlink(nvapi64_dll)
                if os.path.exists(nvapi32_dll):
                    os.unlink(nvapi32_dll)

            # Try to detect known DLLs that ship with the NVIDIA Linux Driver
            # and add them into the prefix
            nvidia_wine_dll_dir = find_nvidia_wine_dll_dir()
            if nvidia_wine_dll_dir:
                for dll in ["_nvngx.dll", "nvngx.dll"]:
                    try_copy(nvidia_wine_dll_dir + "/" + dll,
                             self.prefix_dir + "drive_c/windows/system32/" + dll,
                             optional=True)

            try_copy(g_proton.lib64_dir + "wine/vkd3d-proton/d3d12.dll",
                    self.prefix_dir + "drive_c/windows/system32/d3d12.dll")
            try_copy(g_proton.lib_dir + "wine/vkd3d-proton/d3d12.dll",
                    self.prefix_dir + "drive_c/windows/syswow64/d3d12.dll")

            gamedrive_path = self.prefix_dir + "dosdevices/s:"
            if "gamedrive" in g_session.compat_config:
                library_dir = try_get_game_library_dir()
                if not library_dir:
                    if os.path.lexists(gamedrive_path):
                        os.remove(gamedrive_path)
                else:
                    if os.path.lexists(gamedrive_path):
                        cur_tgt = os.readlink(gamedrive_path)
                        if cur_tgt != library_dir:
                            os.remove(gamedrive_path)
                            os.symlink(library_dir, gamedrive_path)
                    else:
                        os.symlink(library_dir, gamedrive_path)
            elif os.path.lexists(gamedrive_path):
                os.remove(gamedrive_path)

def comma_escaped(s):
    escaped = False
    idx = -1
    while s[idx] == '\\':
        escaped = not escaped
        idx = idx - 1
    return escaped

class Session:
    def __init__(self):
        self.log_file = None
        self.env = dict(os.environ)
        self.dlloverrides = {
                "steam.exe": "b", #always use our special built-in steam.exe
                "dotnetfx35.exe": "b", #replace the broken installer, as does Windows
        }

        self.compat_config = set()
        self.cmdlineappend = []

        if "STEAM_COMPAT_CONFIG" in os.environ:
            config = os.environ["STEAM_COMPAT_CONFIG"]

            while config:
                (cur, sep, config) = config.partition(',')
                if cur.startswith("cmdlineappend:"):
                    while comma_escaped(cur):
                        (a, b, c) = config.partition(',')
                        cur = cur[:-1] + ',' + a
                        config = c
                    self.cmdlineappend.append(cur[14:].replace('\\\\','\\'))
                else:
                    self.compat_config.add(cur)

        #turn forcelgadd on by default unless it is disabled in compat config
        if not "noforcelgadd" in self.compat_config:
            self.compat_config.add("forcelgadd")

    def init_wine(self):
        if "HOST_LC_ALL" in self.env and len(self.env["HOST_LC_ALL"]) > 0:
            #steam sets LC_ALL=C to help some games, but Wine requires the real value
            #in order to do path conversion between win32 and host. steam sets
            #HOST_LC_ALL to allow us to use the real value.
            self.env["LC_ALL"] = self.env["HOST_LC_ALL"]
        else:
            self.env.pop("LC_ALL", "")

        self.env.pop("WINEARCH", "")

        if 'ORIG_'+ld_path_var not in os.environ:
            # Allow wine to restore this when calling an external app.
            self.env['ORIG_'+ld_path_var] = os.environ.get(ld_path_var, '')

        prepend_to_env_str(self.env, ld_path_var, g_proton.lib64_dir + ":" + g_proton.lib_dir, ":")

        self.env["WINEDLLPATH"] = g_proton.lib64_dir + "/wine:" + g_proton.lib_dir + "/wine"

        self.env["GST_PLUGIN_SYSTEM_PATH_1_0"] = g_proton.lib64_dir + "gstreamer-1.0" + ":" + g_proton.lib_dir + "gstreamer-1.0"
        self.env["WINE_GST_REGISTRY_DIR"] = g_compatdata.path("gstreamer-1.0/")

        if "STEAM_COMPAT_MEDIA_PATH" in os.environ:
            self.env["MEDIACONV_AUDIO_DUMP_FILE"] = os.environ["STEAM_COMPAT_MEDIA_PATH"] + "/audio.foz"
            self.env["MEDIACONV_VIDEO_DUMP_FILE"] = os.environ["STEAM_COMPAT_MEDIA_PATH"] + "/video.foz"

        if "STEAM_COMPAT_TRANSCODED_MEDIA_PATH" in os.environ:
            self.env["MEDIACONV_AUDIO_TRANSCODED_FILE"] = os.environ["STEAM_COMPAT_TRANSCODED_MEDIA_PATH"] + "/transcoded_audio.foz"
            self.env["MEDIACONV_VIDEO_TRANSCODED_FILE"] = os.environ["STEAM_COMPAT_TRANSCODED_MEDIA_PATH"] + "/transcoded_video.foz"

        prepend_to_env_str(self.env, "PATH", g_proton.bin_dir, ":")

    def check_environment(self, env_name, config_name):
        if not env_name in self.env:
            return False
        if nonzero(self.env[env_name]):
            self.compat_config.add(config_name)
        else:
            self.compat_config.discard(config_name)
        return True

    def try_log_slr_versions(self):
        try:
            if "PRESSURE_VESSEL_RUNTIME_BASE" in self.env:
                with open(self.env["PRESSURE_VESSEL_RUNTIME_BASE"] + "/VERSIONS.txt", "r") as f:
                    for l in f:
                        l = l.strip()
                        if len(l) > 0 and not l.startswith("#"):
                            cleaned = l.split("#")[0].strip().replace("\t", " ")
                            split = cleaned.split(" ", maxsplit=1)
                            self.log_file.write(split[0] + ": " + split[1] + "\n")
        except (OSError, IOError, TypeError, KeyError):
            pass

    def setup_logging(self, append_forever=False):
        basedir = self.env.get("PROTON_LOG_DIR", os.environ["HOME"])

        if append_forever:
            #SteamGameId is not always available
            lfile_path = basedir + "/steam-proton.log"
        else:
            if not "SteamGameId" in os.environ:
                return False

            lfile_path = basedir + "/steam-" + os.environ["SteamGameId"] + ".log"

            if os.path.exists(lfile_path):
                os.remove(lfile_path)

        makedirs(basedir)
        self.log_file = open(lfile_path, "a")
        return True

    def init_session(self, update_prefix_files):
        self.env["WINEPREFIX"] = g_compatdata.prefix_dir

        #load environment overrides
        used_user_settings = {}
        if os.path.exists(g_proton.user_settings_file):
            try:
                import user_settings
                for key, value in user_settings.user_settings.items():
                    if not key in self.env:
                        self.env[key] = value
                        used_user_settings[key] = value
            except:
                log("************************************************")
                log("THERE IS AN ERROR IN YOUR user_settings.py FILE:")
                log("%s" % sys.exc_info()[1])
                log("************************************************")

        if "PROTON_LOG" in self.env and nonzero(self.env["PROTON_LOG"]):
            self.env.setdefault("WINEDEBUG", "+timestamp,+pid,+tid,+seh,+debugstr,+loaddll,+mscoree")
            self.env.setdefault("DXVK_LOG_LEVEL", "info")
            self.env.setdefault("VKD3D_DEBUG", "warn")
            self.env.setdefault("WINE_MONO_TRACE", "E:System.NotImplementedException")

        #for performance, logging is disabled by default; override with user_settings.py
        self.env.setdefault("WINEDEBUG", "-all")
        self.env.setdefault("DXVK_LOG_LEVEL", "none")
        self.env.setdefault("VKD3D_DEBUG", "none")

        #disable XIM support until libx11 >= 1.7 is widespread
        self.env.setdefault("WINE_ALLOW_XIM", "0")

        if "wined3d11" in self.compat_config:
            self.compat_config.add("wined3d")

        if not self.check_environment("PROTON_USE_WINED3D", "wined3d"):
            self.check_environment("PROTON_USE_WINED3D11", "wined3d")
        self.check_environment("PROTON_NO_D3D12", "nod3d12")
        self.check_environment("PROTON_NO_D3D11", "nod3d11")
        self.check_environment("PROTON_NO_D3D10", "nod3d10")
        self.check_environment("PROTON_NO_D9VK",  "nod3d9")
        self.check_environment("PROTON_NO_ESYNC", "noesync")
        self.check_environment("PROTON_NO_FSYNC", "nofsync")
        self.check_environment("PROTON_FORCE_LARGE_ADDRESS_AWARE", "forcelgadd")
        self.check_environment("PROTON_OLD_GL_STRING", "oldglstr")
        self.check_environment("PROTON_NO_WRITE_WATCH", "nowritewatch")
        self.check_environment("PROTON_HIDE_NVIDIA_GPU", "hidenvgpu")
        self.check_environment("PROTON_SET_GAME_DRIVE", "gamedrive")
        self.check_environment("PROTON_NO_XIM", "noxim")
        self.check_environment("PROTON_HEAP_DELAY_FREE", "heapdelayfree")
        self.check_environment("PROTON_ENABLE_NVAPI", "enablenvapi")
        self.check_environment("PROTON_VKD3D_BINDLESS", "vkd3dbindlesstb")

        if "noesync" in self.compat_config:
            self.env.pop("WINEESYNC", "")
        else:
            self.env["WINEESYNC"] = "1" if "SteamGameId" in self.env else "0"

        if "nofsync" in self.compat_config:
            self.env.pop("WINEFSYNC", "")
        else:
            self.env["WINEFSYNC"] = "1" if "SteamGameId" in self.env else "0"

        if not "noxim" in self.compat_config:
            self.env.pop("WINE_ALLOW_XIM")

        if "nowritewatch" in self.compat_config:
            self.env["WINE_DISABLE_WRITE_WATCH"] = "1"

        if "oldglstr" in self.compat_config:
            #mesa override
            self.env["MESA_EXTENSION_MAX_YEAR"] = "2003"
            #nvidia override
            self.env["__GL_ExtensionStringVersion"] = "17700"

        if "forcelgadd" in self.compat_config:
            self.env["WINE_LARGE_ADDRESS_AWARE"] = "1"

        if "heapdelayfree" in self.compat_config:
            self.env["WINE_HEAP_DELAY_FREE"] = "1"

        if "vkd3dbindlesstb" in self.compat_config:
            append_to_env_str(self.env, "VKD3D_CONFIG", "force_bindless_texel_buffer", ",")

        if "vkd3dfl12" in self.compat_config:
            if not "VKD3D_FEATURE_LEVEL" in self.env:
                self.env["VKD3D_FEATURE_LEVEL"] = "12_0"

        if "hidenvgpu" in self.compat_config:
            self.env["WINE_HIDE_NVIDIA_GPU"] = "1"

        if "PROTON_CRASH_REPORT_DIR" in self.env:
            self.env["WINE_CRASH_REPORT_DIR"] = self.env["PROTON_CRASH_REPORT_DIR"]

        if self.env["WINEDEBUG"] != "-all":
            if self.setup_logging(append_forever=False):
                self.log_file.write("======================\n")
                with open(g_proton.version_file, "r") as f:
                    self.log_file.write("Proton: " + f.readline().strip() + "\n")
                if "SteamGameId" in self.env:
                    self.log_file.write("SteamGameId: " + self.env["SteamGameId"] + "\n")
                self.log_file.write("Command: " + str(sys.argv[2:] + self.cmdlineappend) + "\n")
                self.log_file.write("Options: " + str(self.compat_config) + "\n")

                self.try_log_slr_versions()

                try:
                    uname = os.uname()
                    kernel_version = f"{uname.sysname} {uname.release} {uname.version} {uname.machine}"
                except OSError:
                    kernel_version = "unknown"

                self.log_file.write(f"Kernel: {kernel_version}\n")

                #dump some important variables into the log header
                for var in ["WINEDLLOVERRIDES", "WINEDEBUG"]:
                    if var in os.environ:
                        self.log_file.write("System " + var + ": " + os.environ[var] + "\n")
                    if var in used_user_settings:
                        self.log_file.write("User settings " + var + ": " + used_user_settings[var] + "\n")

                self.log_file.write("======================\n")
                self.log_file.flush()
            else:
                self.env["WINEDEBUG"] = "-all"

        if "PROTON_REMOTE_DEBUG_CMD" in self.env:
            self.remote_debug_cmd = self.env.get("PROTON_REMOTE_DEBUG_CMD").split(" ")
        else:
            self.remote_debug_cmd = None

        if update_prefix_files:
            g_compatdata.setup_prefix()

        if "nod3d12" in self.compat_config:
            self.dlloverrides["d3d12"] = ""
            if "dxgi" in self.dlloverrides:
                del self.dlloverrides["dxgi"]

        if "nod3d11" in self.compat_config:
            self.dlloverrides["d3d11"] = ""
            if "dxgi" in self.dlloverrides:
                del self.dlloverrides["dxgi"]

        if "nod3d10" in self.compat_config:
            self.dlloverrides["d3d10_1"] = ""
            self.dlloverrides["d3d10"] = ""
            self.dlloverrides["dxgi"] = ""

        if "nativevulkanloader" in self.compat_config:
            self.dlloverrides["vulkan-1"] = "n"

        if "nod3d9" in self.compat_config:
            self.dlloverrides["d3d9"] = ""
            self.dlloverrides["dxgi"] = ""

        s = ""
        for dll in self.dlloverrides:
            setting = self.dlloverrides[dll]
            if len(s) > 0:
                s = s + ";" + dll + "=" + setting
            else:
                s = dll + "=" + setting
        append_to_env_str(self.env, "WINEDLLOVERRIDES", s, ";")

    def dump_dbg_env(self, f):
        f.write("PATH=\"" + self.env["PATH"] + "\" \\\n")
        f.write("\tTERM=\"xterm\" \\\n") #XXX
        f.write("\tWINEDEBUG=\"-all\" \\\n")
        f.write("\tWINEDLLPATH=\"" + self.env["WINEDLLPATH"] + "\" \\\n")
        f.write("\t" + ld_path_var + "=\"" + self.env[ld_path_var] + "\" \\\n")
        f.write("\tWINEPREFIX=\"" + self.env["WINEPREFIX"] + "\" \\\n")
        if "WINEESYNC" in self.env:
            f.write("\tWINEESYNC=\"" + self.env["WINEESYNC"] + "\" \\\n")
        if "WINEFSYNC" in self.env:
            f.write("\tWINEFSYNC=\"" + self.env["WINEFSYNC"] + "\" \\\n")
        if "SteamGameId" in self.env:
            f.write("\tSteamGameId=\"" + self.env["SteamGameId"] + "\" \\\n")
        if "SteamAppId" in self.env:
            f.write("\tSteamAppId=\"" + self.env["SteamAppId"] + "\" \\\n")
        if "WINEDLLOVERRIDES" in self.env:
            f.write("\tWINEDLLOVERRIDES=\"" + self.env["WINEDLLOVERRIDES"] + "\" \\\n")
        if "STEAM_COMPAT_CLIENT_INSTALL_PATH" in self.env:
            f.write("\tSTEAM_COMPAT_CLIENT_INSTALL_PATH=\"" + self.env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] + "\" \\\n")
        if "WINE_LARGE_ADDRESS_AWARE" in self.env:
            f.write("\tWINE_LARGE_ADDRESS_AWARE=\"" + self.env["WINE_LARGE_ADDRESS_AWARE"] + "\" \\\n")
        if "GST_PLUGIN_SYSTEM_PATH_1_0" in self.env:
            f.write("\tGST_PLUGIN_SYSTEM_PATH_1_0=\"" + self.env["GST_PLUGIN_SYSTEM_PATH_1_0"] + "\" \\\n")
        if "WINE_GST_REGISTRY_DIR" in self.env:
            f.write("\tWINE_GST_REGISTRY_DIR=\"" + self.env["WINE_GST_REGISTRY_DIR"] + "\" \\\n")
        if "MEDIACONV_AUDIO_DUMP_FILE" in self.env:
            f.write("\tMEDIACONV_AUDIO_DUMP_FILE=\"" + self.env["MEDIACONV_AUDIO_DUMP_FILE"] + "\" \\\n")
        if "MEDIACONV_AUDIO_TRANSCODED_FILE" in self.env:
            f.write("\tMEDIACONV_AUDIO_TRANSCODED_FILE=\"" + self.env["MEDIACONV_AUDIO_TRANSCODED_FILE"] + "\" \\\n")
        if "MEDIACONV_VIDEO_DUMP_FILE" in self.env:
            f.write("\tMEDIACONV_VIDEO_DUMP_FILE=\"" + self.env["MEDIACONV_VIDEO_DUMP_FILE"] + "\" \\\n")
        if "MEDIACONV_VIDEO_TRANSCODED_FILE" in self.env:
            f.write("\tMEDIACONV_VIDEO_TRANSCODED_FILE=\"" + self.env["MEDIACONV_VIDEO_TRANSCODED_FILE"] + "\" \\\n")

    def dump_dbg_scripts(self):
        exe_name = os.path.basename(sys.argv[2])

        tmpdir = self.env.get("PROTON_DEBUG_DIR", "/tmp") + "/proton_" + os.environ["USER"] + "/"
        makedirs(tmpdir)

        with open(tmpdir + "winedbg", "w") as f:
            f.write("#!/bin/bash\n")
            f.write("#Run winedbg with args\n\n")
            f.write("cd \"" + os.getcwd() + "\"\n")
            self.dump_dbg_env(f)
            f.write("\t\"" + g_proton.wine64_bin + "\" winedbg \"$@\"\n")
        os.chmod(tmpdir + "winedbg", 0o755)

        with open(tmpdir + "winedbg_run", "w") as f:
            f.write("#!/bin/bash\n")
            f.write("#Run winedbg and prepare to run game or given program\n\n")
            f.write("cd \"" + os.getcwd() + "\"\n")
            f.write("DEF_CMD=(")
            first = True
            for arg in sys.argv[2:]:
                if first:
                    f.write("\"" + arg + "\"")
                    first = False
                else:
                    f.write(" \"" + arg + "\"")
            f.write(")\n")
            self.dump_dbg_env(f)
            f.write("\t\"" + g_proton.wine64_bin + "\" winedbg \"${@:-${DEF_CMD[@]}}\"\n")
        os.chmod(tmpdir + "winedbg_run", 0o755)

        with open(tmpdir + "gdb_attach", "w") as f:
            f.write("#!/bin/bash\n")
            f.write("#Run winedbg in gdb mode and auto-attach to already-running program\n\n")
            f.write("cd \"" + os.getcwd() + "\"\n")
            f.write("EXE_NAME=${1:-\"" + exe_name + "\"}\n")
            f.write("WPID_HEX=$(\"" + tmpdir + "winedbg\" --command 'info process' | grep -i \"$EXE_NAME\" | cut -f2 -d' ' | sed -e 's/^0*//')\n")
            f.write("if [ -z \"$WPID_HEX\" ]; then \n")
            f.write("    echo \"Program does not appear to be running: \\\"$EXE_NAME\\\"\"\n")
            f.write("    exit 1\n")
            f.write("fi\n")
            f.write("WPID_DEC=$(printf %d 0x$WPID_HEX)\n")
            self.dump_dbg_env(f)
            f.write("\t\"" + g_proton.wine64_bin + "\" winedbg --gdb $WPID_DEC\n")
        os.chmod(tmpdir + "gdb_attach", 0o755)

        with open(tmpdir + "gdb_run", "w") as f:
            f.write("#!/bin/bash\n")
            f.write("#Run winedbg in gdb mode and prepare to run game or given program\n\n")
            f.write("cd \"" + os.getcwd() + "\"\n")
            f.write("DEF_CMD=(")
            first = True
            for arg in sys.argv[2:]:
                if first:
                    f.write("\"" + arg + "\"")
                    first = False
                else:
                    f.write(" \"" + arg + "\"")
            f.write(")\n")
            self.dump_dbg_env(f)
            f.write("\t\"" + g_proton.wine64_bin + "\" winedbg --gdb \"${@:-${DEF_CMD[@]}}\"\n")
        os.chmod(tmpdir + "gdb_run", 0o755)

        with open(tmpdir + "run", "w") as f:
            f.write("#!/bin/bash\n")
            f.write("#Run game or given command in environment\n\n")
            f.write("cd \"" + os.getcwd() + "\"\n")
            f.write("DEF_CMD=(")
            first = True
            for arg in sys.argv[2:]:
                if first:
                    f.write("\"" + arg + "\"")
                    first = False
                else:
                    f.write(" \"" + arg + "\"")
            f.write(")\n")
            self.dump_dbg_env(f)
            f.write("\t\"" + g_proton.wine64_bin + "\" steam.exe \"${@:-${DEF_CMD[@]}}\"\n")
        os.chmod(tmpdir + "run", 0o755)

    def run_proc(self, args, local_env=None):
        if local_env is None:
            local_env = self.env
        subprocess.call(args, env=local_env, stderr=self.log_file, stdout=self.log_file)

    def run(self):
        if "PROTON_DUMP_DEBUG_COMMANDS" in self.env and nonzero(self.env["PROTON_DUMP_DEBUG_COMMANDS"]):
            try:
                self.dump_dbg_scripts()
            except OSError:
                log("Unable to write debug scripts! " + str(sys.exc_info()[1]))

        if self.remote_debug_cmd:
            remote_debug_proc = subprocess.Popen([g_proton.wine54_bin] + self.remote_debug_cmd,
                                                 env=self.env, stderr=self.log_file, stdout=self.log_file)
        else:
            remote_debug_proc = None

        commandstring = str(sys.argv[2:] + self.cmdlineappend)

        # use a string check instead of single string in case we need to check for more in the future
        check_args = [
            'iscriptevaluator.exe' in commandstring,
            'getcompatpath' in  str(sys.argv[1]),
            'getnativepath' in  str(sys.argv[1]),
        ]

        if any(check_args):
            self.run_proc([g_proton.wine64_bin, "start.exe", "/exec", "steam"] + sys.argv[2:] + self.cmdlineappend)
            #commandstring = str([g_proton.wine64_bin, "start.exe", "/exec", "steam"] + sys.argv[2:] + self.cmdlineappend)
        else:
            self.run_proc([g_proton.wine64_bin, "start.exe", "/b", "steam"] + sys.argv[2:] + self.cmdlineappend)
            #commandstring = str([g_proton.wine64_bin, "start.exe", "/b", "steam"] + sys.argv[2:] + self.cmdlineappend)

        #log("Running: " + commandstring)

        if remote_debug_proc:
            remote_debug_proc.kill()
            try:
                remote_debug_proc.communicate(2)
            except subprocess.TimeoutExpired as e:
                log("terminate remote debugger")
                remote_debug_proc.terminate()
                remote_debug_proc.communicate()

if __name__ == "__main__":
    if not "STEAM_COMPAT_DATA_PATH" in os.environ:
        log("No compat data path?")
        sys.exit(1)

    g_proton = Proton(os.path.dirname(sys.argv[0]))

    g_proton.cleanup_legacy_dist()
    g_proton.do_steampipe_fixups()

    g_compatdata = CompatData(os.environ["STEAM_COMPAT_DATA_PATH"])

    g_session = Session()

    #install stupid EA Origin if need.
    if "link2ea" in str(sys.argv[2]):
        os.environ["ORIGIN"] = "1"

    g_session.init_wine()

    if g_proton.missing_default_prefix():
        g_proton.make_default_prefix()

    import protonfixes

    g_session.init_session(sys.argv[1] != "runinprefix")

    #determine mode
    if sys.argv[1] == "run":
        #start target app
        g_session.run()
    elif sys.argv[1] == "waitforexitandrun":
        #wait for wineserver to shut down
        g_session.run_proc([g_proton.wineserver_bin, "-w"])
        #then run
        g_session.run()
    elif sys.argv[1] == "runinprefix":
        g_session.run_proc([g_proton.wine_bin] + sys.argv[2:])
    elif sys.argv[1] == "getcompatpath":
        #linux -> windows path
        path = subprocess.check_output([g_proton.wine64_bin, "winepath", "-w", sys.argv[2]], env=g_session.env, stderr=g_session.log_file)
        sys.stdout.buffer.write(path)
    elif sys.argv[1] == "getnativepath":
        #windows -> linux path
        path = subprocess.check_output([g_proton.wine64_bin, "winepath", sys.argv[2]], env=g_session.env, stderr=g_session.log_file)
        sys.stdout.buffer.write(path)
    else:
        log("Need a verb.")
        sys.exit(1)

    sys.exit(0)

#pylint --disable=C0301,C0326,C0330,C0111,C0103,R0902,C1801,R0914,R0912,R0915
# vim: set syntax=python:
