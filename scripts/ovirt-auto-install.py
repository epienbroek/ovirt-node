#!/usr/bin/env python
#
# ovirt-auto-install.py - Copyright (C) 2011 Red Hat, Inc.
# Written by Joey Boggs <jboggs@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.  A copy of the GNU General Public License is
# also available at http://www.gnu.org/copyleft/gpl.html.

from ovirtnode.ovirtfunctions import *
from ovirtnode.storage import *
from ovirtnode.install import *
from ovirtnode.network import *
from ovirtnode.log import *
from ovirt.node.utils.console import TransactionProgress
from ovirt.node.utils import system
from ovirt.node.utils import security, storage, hooks
from ovirt.node.config import defaults
from ovirt.node.utils.system import which, kernel_cmdline_arguments, \
    SystemRelease
import logging
import sys
import os
import time


class PrepareInstallation(Transaction.Element):
    title = "Prepare installation"

    def commit(self):
        defaults.ConfigVersion().set_to_current()


class ConfigureNetworking(Transaction.Element):
    title = "Configuring network"

    def commit(self):
        build_network_auto_transaction().run()


class AutomaticDiskPartitioning(Transaction.Element):
    title = "Performing automatic disk partitioning"

    def commit(self):
        if storage_auto():
            # store /etc/shadow if adminpw/rootpw are set,
            # handled already in ovirt-early
            file = open("/proc/cmdline")
            args = file.read()
            if "adminpw" in args or "rootpw" in args:
                print "Storing /etc/shadow"
                ovirt_store_config("/etc/passwd")
                ovirt_store_config("/etc/shadow")
            file.close()
        else:
            raise RuntimeError("Automatic installation failed. " +
                               "Please review /var/log/ovirt.log")


class EnableSshPasswordAuthentication(Transaction.Element):
    title = "Enabling SSH password authentication"

    def commit(self):
        if OVIRT_VARS["OVIRT_SSH_PWAUTH"] == "yes":
            augtool("set",
                    "/files/etc/ssh/sshd_config/PasswordAuthentication",
                    "yes")
        elif OVIRT_VARS["OVIRT_SSH_PWAUTH"] == "no":
            augtool("set",
                    "/files/etc/ssh/sshd_config/PasswordAuthentication",
                    "no")
        ovirt_store_config("/etc/ssh/sshd_config")
        system_closefds("service sshd restart &> /dev/null")


class SetKeyboardLayout(Transaction.Element):
    title = "Setting Keyboard Layout"

    def commit(self):
        try:
            model = defaults.Keyboard()
            model.update(layout=OVIRT_VARS["OVIRT_KEYBOARD_LAYOUT"])
            tx = model.transaction()
            tx()
        except:
            logger.warning("Unknown keyboard layout: %s" % \
                           OVIRT_VARS["OVIRT_KEYBOARD_LAYOUT"])


class ConfigureStrongRNG(Transaction.Element):
    title = "Configuring SSH strong RNG"

    def commit(self):
        try:
            model = defaults.SSH()
            model.update(num_bytes=OVIRT_VARS["OVIRT_USE_STRONG_RNG"])
            tx = model.transaction()
            tx()
        except:
            logger.warning("Unknown ssh strong RNG: %s" % \
                           OVIRT_VARS["OVIRT_USE_STRONG_RNG"])


class ConfigureAESNI(Transaction.Element):
    title = "Configuring SSH AES NI"

    def commit(self):
        try:
            model = defaults.SSH()
            model.update(disable_aesni=True)
            tx = model.transaction()
            tx()
        except:
            logger.warning("Unknown ssh AES NI: %s" % \
                           OVIRT_VARS["OVIRT_DISABLE_AES_NI"])


class ConfigureNfsv4(Transaction.Element):
    title = "Setting NFSv4 domain"

    def commit(self):
        try:
            model = defaults.NFSv4()
            model.update(domain=OVIRT_VARS["OVIRT_NFSV4_DOMAIN"])
            tx = model.transaction()
            tx()
        except:
            logger.warning("Unknown NFSv4 domain: %s" % \
                           OVIRT_VARS["OVIRT_NFSV4_DOMAIN"])


class ConfigureLogging(Transaction.Element):
    title = "Configuring Logging"

    def commit(self):
        logging_auto()


class ConfigureCollectd(Transaction.Element):
    title = "Configuring Collectd"

    def commit(self):
        try:
            from ovirt_config_setup.collectd import collectd_auto
            collectd_auto()
        except:
            pass


class PerformInstallation(Transaction.Element):
    title = "Transferring image"

    def commit(self):
        Install()


class ConfigureKdump(Transaction.Element):
    title = "Configuring KDump"

    def commit(self):
        try:
            model = defaults.KDump()

            if "OVIRT_KDUMP_SSH" in OVIRT_VARS and \
                    "OVIRT_KDUMP_SSH_KEY" in OVIRT_VARS:
                model.configure_ssh(OVIRT_VARS["OVIRT_KDUMP_SSH"],
                                    OVIRT_VARS["OVIRT_KDUMP_SSH_KEY"])
            elif "OVIRT_KDUMP_NFS" in OVIRT_VARS:
                model.configure_nfs(OVIRT_VARS["OVIRT_KDUMP_NFS"])
            elif "OVIRT_DISABLE_KDUMP" in OVIRT_VARS:
                model.configure_disable()
            else:
                model.configure_local()

            tx = model.transaction()
            tx()

        except:
            kdump_args = ["OVIRT_KDUMP_SSH", "OVIRT_KDUMP_SSH_KEY",
                          "OVIRT_KDUMP_NFS", "OVIRT_KDUMP_LOCAL"]
            logger.warning("Unknown kdump configuration: %s" % \
                            " ".join([x for x in kdump_args if \
                                      x in OVIRT_VARS]))


class InstallBootloader(Transaction.Element):
    title = "Installing Bootloader"

    def commit(self):
        # FIXME
        # This is a hack because the legacy code messes with
        # the config file, so we backup and replay it later
        cfgfile = defaults.NodeConfigFile()
        cfg = cfgfile.get_dict()

        install = Install()
        if not install.ovirt_boot_setup():
            raise RuntimeError("Bootloader Installation Failed")
        cfgfile.write(cfg)


class RunHooks(Transaction.Element):
    """Run post-install hooks
    e.g. to avoid reboot loops using Cobbler PXE only once
    Cobbler XMLRPC post-install trigger (XXX is there cobbler SRV record?):
    wget "http://192.168.50.2/cblr/svc/op/trig/mode/post/system/$(hostname)"
      -O /dev/null
    """
    title = "Running Hooks"

    def commit(self):
        hooks.Hooks.post_auto_install()


if __name__ == "__main__":
    if "--debug" in sys.argv:
        logging.basicConfig(level=logging.DEBUG)

    tx = Transaction("Automatic Installation")

    tx.append(PrepareInstallation())

    # setup network before storage for iscsi installs
    if is_iscsi_install():
        tx.append(ConfigureNetworking())

    if not is_stateless():
        tx.append(AutomaticDiskPartitioning())

    if not is_iscsi_install():
        tx.append(ConfigureNetworking())

    #set ssh_passwd_auth
    if "OVIRT_SSH_PWAUTH" in OVIRT_VARS:
        tx.append(EnableSshPasswordAuthentication())

    #set keyboard_layout
    if "OVIRT_KEYBOARD_LAYOUT" in OVIRT_VARS and \
       not OVIRT_VARS["OVIRT_KEYBOARD_LAYOUT"] is "":
        tx.append(SetKeyboardLayout())

    #set ssh strong RHG
    if "OVIRT_USE_STRONG_RNG" in OVIRT_VARS and \
       not OVIRT_VARS["OVIRT_USE_STRONG_RNG"] is "":
        tx.append(ConfigureStrongRNG())

    #set ssh AES NI
    if "OVIRT_DISABLE_AES_NI" in OVIRT_VARS and \
       OVIRT_VARS["OVIRT_DISABLE_AES_NI"] == "true":
        tx.append(ConfigureAESNI())

    #set NFSv4 domain
    if "OVIRT_NFSV4_DOMAIN" in OVIRT_VARS and \
       not OVIRT_VARS["OVIRT_NFSV4_DOMAIN"] is "":
        tx.append(ConfigureNfsv4())

    tx.append(ConfigureLogging())

    if not SystemRelease().is_el():
        tx.append(ConfigureCollectd())

    tx.append(PerformInstallation())  # FIXME needed??

    tx.append(ConfigureKdump())

    if not is_stateless():
        tx.append(InstallBootloader())

    tx.append(RunHooks())

    TransactionProgress(tx, is_dry=False).run()
    print "Installation and Configuration Completed"

    reboot_delay = kernel_cmdline_arguments().get("reboot_delay", None)
    if reboot_delay:
        print "Reboot Scheduled in %s seconds later" % reboot_delay
        time.sleep(int(reboot_delay))
        os.system(which("reboot"))

    # python will exit with 1 if an exception occurs
