# -=- encoding: utf-8 -=-
#
# SFLvault - Secure networked password store and credentials manager.
#
# Copyright (C) 2008  Savoir-faire Linux inc.
#
# Author: Alexandre Bourget <alexandre.bourget@savoirfairelinux.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

__version__ = __import__('pkg_resources').get_distribution('SFLvault').version

from ConfigParser import ConfigParser

import xmlrpclib
import getpass
import sys
import re
import os
from subprocess import Popen, PIPE

from decorator import decorator
from pprint import pprint

from sflvault.lib.common import VaultError
from sflvault.lib.common.crypto import *
from sflvault.client.utils import *
from sflvault.client import remoting


# Default configuration

# Default configuration file
CONFIG_FILE = '~/.sflvault/config'
# Environment variable to override default config file.
CONFIG_FILE_ENV = 'SFLVAULT_CONFIG'



### Setup variables and functions


def vaultReply(rep, errmsg="Error"):
    """Tracks the Vault reply, and raise an Exception on error"""

    if rep['error']:
        print "%s: %s" % (errmsg, rep['message'])
        raise VaultError(rep['message'])
    
    return rep


#
# authenticate decorator
#
def authenticate(keep_privkey=False):
    def do_authenticate(func, self, *args, **kwargs):
        """Login decorator
        
        self is there because it's called on class elements.
        """

        username = self.cfg.get('SFLvault', 'username')
        privkey = None

        # Check if we've cached the decrypted private key
        if hasattr(self, 'privkey'):
            # Use cached private key.
            privkey = self.privkey

        else:

            try:
                privkey_enc = self.cfg.get('SFLvault', 'key')
            except:
                raise VaultConfigurationError("No private key in local config, init with: user-setup username vault-url")
        
            try:
                privpass = self.getpassfunc()
                privkey_packed = decrypt_privkey(privkey_enc, privpass)
                del(privpass)
                eg = ElGamal.ElGamalobj()
                (eg.p, eg.x, eg.g, eg.y) = unserial_elgamal_privkey(privkey_packed)
                privkey = eg

            except DecryptError, e:
                print "[SFLvault] Invalid passphrase"
                return False
            except KeyboardInterrupt, e:
                print "[aborted]"
                return False

            # When we ask to keep the privkey, keep the ElGamal obj.
            if keep_privkey or self.shell_mode:
                self.privkey = privkey


        # Go for the login/authenticate roundtrip

        # TODO: check also is the privkey (ElGamal obj) has been cached
        #       in self.privkey (when invoked with keep_privkey)
        retval = self.vault.login(username)
        self.authret = retval
        if not retval['error']:
            # decrypt token.

            cryptok = privkey.decrypt(unserial_elgamal_msg(retval['cryptok']))
            retval2 = self.vault.authenticate(username, b64encode(cryptok))
            self.authret = retval2
        
            if retval2['error']:
                raise AuthenticationError("Authentication failed: %s" % \
                                          retval2['message'])
            else:
                self.authtok = retval2['authtok']
                print "Authentication successful"
        else:
            raise AuthenticationError("Authentication failed: %s" % \
                                      retval['message'])

        return func(self, *args, **kwargs)

    return decorator(do_authenticate)

###
### Différentes façons d'obtenir la passphrase
###
class AskPassMethods(object):
    """Wrapper for askpass methods"""
    
    env_var = 'SFLVAULT_ASKPASS'

    def program(self):
        try:
            p = Popen(args=[self._program_value], shell=False, stdout=PIPE)
            p.wait()
            return p.stdout.read()
        except OSError, e:
            msg = "Failed to run '%s' : %s" % (os.environ[env_var], e)
            raise ValueError(msg)

    def default(self):
        """Default function to get passphrase from user, for authentication."""
        return getpass.getpass("Vault passphrase: ", stream=sys.stderr)
    
    def __init__(self):
        # Default
        self.getpass = self.default

        # Use 'program' is SFLVAULT_ASKPASS env var exists
        env_var = AskPassMethods.env_var
        if env_var in os.environ:
            self._program_value = os.environ[env_var]
            self.getpass = self.program

    

###
### On définit les fonctions qui vont traiter chaque sorte de requête.
###
class SFLvaultClient(object):
    """Main SFLvault Client object.

    Use this object to connect to the vault and script it if necessary.
    
    This is the object all clients will use to communicate with a remote
    or local Vault.
    """
    def __init__(self, cfg=None, shell=False):
        """Set up initial configuration for function calls

        When shell = True, privkey will be cached for a while.
        """

        # Load configuration
        self.config_read()

        # The function to call upon @authenticate to get passphrase from user.
        self.getpassfunc = AskPassMethods().getpass    

        self.shell_mode = shell
        self.authtok = ''
        self.authret = None
        # Set the default route to the Vault
        url = self.cfg.get('SFLvault', 'url')
        if url:
            self.vault = xmlrpclib.Server(url, allow_none=True).sflvault

    def config_check(self, config_file):
        """Checks for ownership and modes for all paths and files, à-la SSH"""
        fullfile = os.path.expanduser(config_file)
        fullpath = os.path.dirname(fullfile)
    
        if not os.path.exists(fullpath):
            os.makedirs(fullpath, mode=0700)

        if not os.stat(fullpath)[0] & 0700:
            ### TODO: RAISE EXCEPTION INSTEAD
            print "Modes for %s must be 0700 (-rwx------)" % fullpath
            sys.exit()

        if not os.path.exists(fullfile):
            fp = open(fullfile, 'w')
            fp.write("[SFLvault]\n")
            fp.close()
            os.chmod(fullfile, 0600)
        
        if not os.stat(fullfile)[0] & 0600:
            # TODO: raise exception instead.
            print "Modes for %s must be 0600 (-rw-------)" % fullfile
            sys.exit()

    @property
    def config_filename(self):
        """Return the configuration filename"""
        if CONFIG_FILE_ENV in os.environ:
            return os.environ[CONFIG_FILE_ENV]
        else:
            return CONFIG_FILE

    def config_read(self):
        """Return the ConfigParser object, fully loaded"""

        self.config_check(self.config_filename)
    
        self.cfg = ConfigParser()
        fp = open(os.path.expanduser(self.config_filename), 'r')
        self.cfg.readfp(fp)
        fp.close()

        if not self.cfg.has_section('SFLvault'):
            self.cfg.add_section('SFLvault')

        if not self.cfg.has_section('Aliases'):
            self.cfg.add_section('Aliases')

        if not self.cfg.has_option('SFLvault', 'username'):
            self.cfg.set('SFLvault', 'username', '')
    
        if not self.cfg.has_option('SFLvault', 'url'):
            self.cfg.set('SFLvault', 'url', '')

    def config_write(self):
        """Write the ConfigParser element to disk."""
        fp = open(os.path.expanduser(self.config_filename), 'w')
        self.cfg.write(fp)
        fp.close()

    def set_getpassfunc(self, func):
        """Set the function to ask for passphrase.

        By default, it is set to _getpass, which asks for the passphrase on the
        command line, but you can create a new function, that would for example
        pop-up a window, or use another mechanism to ask for passphrase and
        continue authentication."""
        self.getpassfunc = func
        
    def _set_vault(self, url, save=False):
        """Set the vault's URL and optionally save it"""
        # When testing, don't tweak the vault.
        if not url:
            return
        
        self.vault = xmlrpclib.Server(url).sflvault
        if save:
            self.cfg.set('SFLvault', 'url', url)


    def alias_add(self, alias, ptr):
        """Add an alias and save config."""

        tid = re.match(r'(.)#(\d+)', ptr)

        if not tid:
            raise ValueError("VaultID must be in the format: (.)#(\d+)")

        # Set the alias value
        self.cfg.set('Aliases', alias, ptr)
        
        # Save config.
        self.config_write()

    def alias_del(self, alias):
        """Remove an alias from the config.

        Return True if removed, False otherwise."""

        if self.cfg.has_option('Aliases', alias):
            self.cfg.remove_option('Aliases', alias)
            self.config_write()
            return True
        else:
            return False

    def alias_list(self):
        """Return a list of aliases"""
        return self.cfg.items('Aliases')

    def alias_get(self, alias):
        """Return the pointer for a given alias"""
        if not self.cfg.has_option('Aliases', alias):
            return None
        else:
            return self.cfg.get('Aliases', alias)


    def vaultId(self, vid, prefix, check_alias=True):
        """Return an integer value for a given VaultID.
        
        A VaultID can be one of the following:
        
        123   - treated as is, and assume to be of type `prefix`.
        m#123 - checked against `prefix`, otherwise raise an exception.
        alias - checked against `prefix` and alias list, returns an int
        value, or raise an exception.
        """
        #prefixes = ['m', 'u', 's', 'c'] # Machine, User, Service, Customer
        #if prefix not in prefixes:
        #    raise ValueError("Bad prefix for id %s (prefix given: %s)" % (id, prefix))
        
        # If it's only a numeric, assume it is of type 'prefix'.
        try:
            tmp = int(vid)
            return tmp
        except:
            pass

        # Match the m#123 formats..
        tid = re.match(r'(.)#(\d+)', vid)
        if tid:
            if tid.group(1) != prefix:
                raise VaultIDSpecError("Bad prefix for VaultID, "\
                                         "context requires '%s': %s"\
                                         % (prefix, vid))
            return int(tid.group(2))

        if check_alias:
            nid = self.alias_get(vid)

            if not nid:
                raise VaultIDSpecError("No such alias '%s'. Use `alias %s s#[ID]` to set." % (vid, vid))

            return self.vaultId(nid, prefix, False)

        raise VaultIDSpecError("Invalid VaultID format: %s" % vid)



    ### REMOTE ACCESS METHODS


    @authenticate()
    def user_add(self, username, admin=False):
        # TODO: add support for --admin, to give admin privileges

        retval = vaultReply(self.vault.user_add(self.authtok, username, admin),
                            "Error adding user")

        print "Success: %s" % retval['message']
        print "New user ID: u#%d" % retval['user_id']


    @authenticate()
    def user_del(self, username):
        retval = vaultReply(self.vault.user_del(self.authtok, username),
                            "Error removing user")

        print "Success: %s" % retval['message']


    def _services_returned(self, retval):
        """Helper function for customer_del, machine_del and service_del."""
        
        if retval['error']:
            print "Error: %s" % retval['message']

            if 'childs' in retval:
                print "Those services rely on services you were going "\
                      "to delete:"
                for x in retval['childs']:
                    print "     s#%s%s%s" % (x['id'],
                                             ' ' * (6 - len(str(x['id']))),
                                             x['url'])
        else:
            print "Success: %s" % retval['message']


    @authenticate()
    def customer_del(self, customer_id):
        retval = self.vault.customer_del(self.authtok, customer_id)

        self._services_returned(retval)
        

    @authenticate()
    def machine_del(self, machine_id):
        retval = self.vault.machine_del(self.authtok, machine_id)

        self._services_returned(retval)
        


    @authenticate(True)
    def customer_get(self, customer_id):
        """Get information to be edited"""
        retval = vaultReply(self.vault.customer_get(self.authtok, customer_id),
                            "Error fetching data for customer %s" % customer_id)

        return retval['customer']

    @authenticate(True)
    def customer_put(self, customer_id, data):
        """Save the (potentially modified) customer to the Vault"""
        retval = vaultReply(self.vault.customer_put(self.authtok, customer_id,
                                                   data),
                            "Error saving data to vault, aborting.")

        print "Success: %s " % retval['message']
        

    @authenticate()
    def service_del(self, service_id):
        retval = self.vault.service_del(self.authtok, service_id)

        self._services_returned(retval)


    @authenticate()
    def customer_add(self, customer_name):
        retval = vaultReply(self.vault.customer_add(self.authtok,
                                                    customer_name),
                            "Error adding customer")

        print "Success: %s" % retval['message']
        print "New customer ID: c#%d" % retval['customer_id']


    @authenticate()
    def machine_add(self, customer_id, name, fqdn, ip, location, notes):
        """Add a machine to the database."""
        # customer_id REQUIRED
        retval = vaultReply(self.vault.machine_add(self.authtok,
                                                   int(customer_id),
                                                   name or '', fqdn or '',
                                                   ip or '', location or '',
                                                   notes or ''),
                            "Error adding machine")
        print "Success: %s" % retval['message']
        print "New machine ID: m#%d" % int(retval['machine_id'])


    @authenticate()
    def service_add(self, machine_id, parent_service_id, url, group_ids, secret,
                    notes):
        """Add a service to the Vault's database.

        machine_id - A m#id machine identifier.
        parent_service_id - A s#id, parent service ID, to which you should
                            connect before connecting to the service you're
                            adding. Specify 0 of None if no parent exist.
                            If you set this, machine_id is disregarded.
        url - URL of the service, with username, port and path if required
        group_ids - Multiple group IDs the service is part of. See `list-groups`
        notes - Simple text field, with notes.
        secret - Password for the service. Plain-text.
        """

        # TODO: accept group_id as group_ids, accept list and send list.

        retval = vaultReply(self.vault.service_add(self.authtok,
                                                  int(machine_id),
                                                  int(parent_service_id),
                                                  url,
                                                  group_ids, secret,
                                                  notes or ''),
                            "Error adding service")

        print "Success: %s" % retval['message']
        print "New service ID: s#%d" % retval['service_id']


    @authenticate()
    def service_passwd(self, service_id, newsecret):
        """Updates the password on the Vault for a certain service"""
        retval = vaultReply(self.vault.service_passwd(self.authtok,
                                                        service_id,
                                                        newsecret),
                            "Error changing password for "\
                            "service %s" % service_id)

        print "Success: %s" % retval['message']
        print "Password updated for service: s#%d" % int(retval['service_id'])
                            
    
    def user_setup(self, username, vault_url, passphrase=None):
        """Sets up the local configuration to communicate with the Vault.

        username  - the name with which an admin prepared (with add-user)
                    your account.
        vault_url - the URL pointing to the XML-RPC interface of the vault
                    (typically host://domain.example.org:5000/vault/rpc
        """
        self._set_vault(vault_url, False)
        
        # Generate a new key:
        print "Generating new ElGamal key-pair..."
        eg = generate_elgamal_keypair()

        # Marshal the ElGamal key
        pubkey = elgamal_pubkey(eg)

        print "You will need a passphrase to secure your private key. The"
        print "encrypted key will be stored on this machine in %s" % self.config_filename
        print '-' * 80

        if not passphrase:
            while True:
                passphrase = getpass.getpass("Enter passphrase (to secure "
                                             "your private key): ")
                passph2 = getpass.getpass("Enter passphrase again: ")

                if passphrase != passph2:
                    print "Passphrase mismatch, try again."
                elif passphrase == '':
                    print "Passphrase cannot be null."
                else:
                    del(passph2)
                    break

        
        print "Sending request to vault..."
        # Send it to the vault, with username
        retval = vaultReply(self.vault.user_setup(username,
                                                serial_elgamal_pubkey(pubkey)),
                            "Setup failed")

        # If Vault sends a SUCCESS, save all the stuff (username, vault_url)
        # and encrypt privkey locally (with Blowfish)
        print "Vault says: %s" % retval['message']

        # Save all (username, vault_url)
        # Encrypt privkey locally (with Blowfish)
        self.cfg.set('SFLvault', 'username', username)
        self._set_vault(vault_url, True)
        # p and x form the private key, add the public key, add g and y.
        # if encryption is required at some point.
        self.cfg.set('SFLvault', 'key',
                   encrypt_privkey(serial_elgamal_privkey(elgamal_bothkeys(eg)),
                                   passphrase))
        del(passphrase)
        del(eg)

        print "Saving settings..."
        self.config_write()


    @authenticate()
    def search(self, query, groups_ids=None, verbose=False):
        """Search the database for query terms, specified as a list of REGEXPs.

        Returns a hierarchical view of the results."""
        retval = vaultReply(self.vault.search(self.authtok, query, groups_ids,
                                              verbose),
                            "Error searching database")

        print "Results:"

        # TODO: call the pager `less` when too long.
        level = 0
        for c_id, c in retval['results'].items():
            level = 0
            # Display customer info
            print "c#%s  %s" % (c_id, c['name'])

            spc1 = ' ' * (4 + len(c_id))
            for m_id, m in c['machines'].items():
                level = 1
                # Display machine infos: 
                add = ' ' * (4 + len(m_id))
                print "%sm#%s  %s (%s - %s)" % (spc1, m_id,
                                                m['name'], m['fqdn'], m['ip'])
                if verbose:
                    print "%s%slocation: %s" % (spc1, add, m['location'])
                    print "%s%snotes: %s" % (spc1, add, m['notes'])
                                                             

                spc2 = spc1 + add
                print ""
                for s_id, s in m['services'].items():
                    level = 2
                    # Display service infos
                    add = ' ' * (4 + len(s_id))
                    p_id = s.get('parent_service_id')
                    print "%ss#%s  %s%s" % (spc2, s_id, s['url'],
                                            ("   (depends: s#%s)" % \
                                             p_id if p_id else ''))
                    if verbose:
                        print "%s%snotes: %s" % (spc2, add, s['notes'])

                if level == 2:
                    print "%s" % (spc2) + '-' * (80 - len(spc2))
                
            if level in [0,1]:
                print "%s" % (spc1) + '-' * (80 - len(spc1))
            
    def _decrypt_service(self, serv, onlysymkey=False, onlygroupkey=False):
        """Decrypt the information return from the vault.

        onlysymkey - return the plain symkey in the result
        onlygroupkey - return the plain groupkey ElGamal obj in result
        """
        # First decrypt groupkey
        try:
            # TODO: implement a groupkey cache system, since it's the longest
            #       thing to decrypt (over a second on a 3GHz machine)
            grouppacked = decrypt_longmsg(self.privkey, serv['cryptgroupkey'])
        except StandardException, e:
            raise DecryptError("Unable to decrypt groupkey (%s)" % e.message)

        eg = ElGamal.ElGamalobj()
        (eg.p, eg.x, eg.g, eg.y) = unserial_elgamal_privkey(grouppacked)
        groupkey = eg
        
        if onlygroupkey:
            serv['groupkey'] = eg
            
        # Then decrypt symkey
        try:
            aeskey = decrypt_longmsg(groupkey, serv['cryptsymkey'])
        except StandardException, e:
            raise DecryptError("Unable to decrypt symkey (%s)" % e.message)

        if onlysymkey:
            serv['symkey'] = aeskey

        if not onlygroupkey and not onlysymkey:
            serv['plaintext'] = decrypt_secret(aeskey, serv['secret'])


    @authenticate(True)
    def service_get(self, service_id):
        """Get information to be edited"""
        retval = vaultReply(self.vault.service_get(self.authtok, service_id),
                            "Error fetching data for service %s" % service_id)

        serv = retval['service']
        # Decrypt secret
        aeskey = ''
        secret = ''

        # Add it only if we can! (or if we want to)
        if 'cryptgroupkey' in serv:
            self._decrypt_service(serv)

        return serv


    @authenticate(True)
    def service_get_tree(self, service_id):
        """Get information to be edited"""
        retval = vaultReply(self.vault.service_get_tree(self.authtok,
                                                        service_id),
                "Error fetching data-tree for service %s" % service_id)

        for x in retval['services']:
            # Decrypt secret
            aeskey = ''
            secret = ''

            if not x['cryptsymkey']:
                # Don't add a plaintext if we can't.
                continue

            self._decrypt_service(x)

        return retval['services']



    @authenticate(True)
    def service_put(self, service_id, data):
        """Save the (potentially modified) service to the Vault"""
        retval = vaultReply(self.vault.service_put(self.authtok, service_id,
                                                   data),
                            "Error saving data to vault, aborting.")

        print "Success: %s " % retval['message']
        

    @authenticate(True)
    def machine_get(self, machine_id):
        """Get information to be edited"""
        retval = vaultReply(self.vault.machine_get(self.authtok, machine_id),
                            "Error fetching data for machine %s" % machine_id)

        return retval['machine']

    @authenticate(True)
    def machine_put(self, machine_id, data):
        """Save the (potentially modified) machine to the Vault"""
        retval = vaultReply(self.vault.machine_put(self.authtok, machine_id,
                                                   data),
                            "Error saving data to vault, aborting.")

        print "Success: %s " % retval['message']
        


    @authenticate(True)
    def show(self, service_id, verbose=False):
        """Show informations to connect to a particular service"""
        servs = self.service_get_tree(service_id)

        print "Results:"

        # TODO: call pager `less` when too long.
        pre = ''
        for x in servs:
            # Show separator
            if pre:
                pass
                #print "%s%s" % (pre, '-' * (80-len(pre)))
                
            spc = len(str(x['id'])) * ' '

            secret = x['plaintext'] if 'plaintext' in x else '[access denied]'
            print "%ss#%d %s" % (pre, x['id'], x['url'])
            print "%s%s   secret: %s" % (pre, spc, secret)
            
            if verbose:
                print "%s%s   notes: %s" % (pre,spc, x['notes'])
            del(secret)

            pre = pre + '   ' + spc


    @authenticate(True)
    def connect(self, vid):
        """Connect to a distant machine (using SSH for now)"""
        servs = self.service_get_tree(vid)

        # Check and decrypt all ciphers prior to start connection,
        # if there are some missing, it's not useful to start.
        for x in servs:
            if not x['cryptsymkey']:
                raise RemotingError("We don't have access to password for service %s" % x['url'])

        connection = remoting.Chain(servs)
        connection.setup()
        connection.connect()

    @authenticate()
    def user_list(self, groups=False):
        """List users

        ``groups`` - if True, list groups for each user also
        """
        # Receive: [{'id': x.id, 'username': x.username,
        #            'created_time': x.created_time,
        #            'is_admin': x.is_admin,
        #            'setup_expired': x.setup_expired()}
        #            {}, {}, ...]
        #    
        retval = vaultReply(self.vault.user_list(self.authtok, groups),
                            "Error listing users")

        print "User list (with creation date):"
        
        to_clean = []  # Expired users to be removed
        for x in retval['list']:
            add = ''
            if x['is_admin']:
                add += ' [global admin]'
            if x['setup_expired']:
                add += ' [setup expired]'
                to_clean.append(x['username'])
            if x['waiting_setup'] and not x['setup_expired']:
                add += ' [in setup process]'

            # TODO: load the xmlrpclib.DateTime object into something more fun
            #       to deal with! Some day..
            print "u#%d\t%s\t%s %s" % (x['id'], x['username'],
                                       x['created_stamp'], add)

            if 'groups' in x:
                for grp in x['groups']:
                    add = ' [admin]' if grp['is_admin'] else ''
                    print "\t\tg#%s\t%s %s" % (grp['id'], grp['name'], add)

        print '-' * 80

        if len(to_clean):
            print "There are expired users. To remove them, run:"
            for usr in to_clean:
                print "   sflvault del-user %s" % usr
        

    @authenticate(True)
    def group_get(self, group_id):
        """Get information to be edited"""
        retval = vaultReply(self.vault.group_get(self.authtok, group_id),
                            "Error fetching data for group %s" % group_id)

        return retval['group']

    @authenticate(True)
    def group_put(self, group_id, data):
        """Save the (potentially modified) Group to the Vault"""
        retval = vaultReply(self.vault.group_put(self.authtok, group_id,
                                                   data),
                            "Error saving data to vault, aborting.")

        print "Success: %s " % retval['message']
        


    @authenticate(True)
    def group_add_service(self, group_id, service_id, retval=None):
        print "Fetching service info..."
        retval = vaultReply(self.vault.service_get(self.authtok, service_id),
                            "Error loading service infos")

        # TODO: decrypt the symkey with the group's decrypted privkey.
        serv = retval['service']

        print "Decrypting symkey..."
        self._decrypt_service(serv, onlysymkey=True)


        print "Sending data back to vault"
        retval = vaultReply(self.vault.group_add_service(self.authtok,
                                                         group_id,
                                                         service_id,
                                                         serv['symkey']),
                            "Error adding service to group")

        print "Success: %s" % retval['message']


    @authenticate()
    def group_del_service(self, group_id, service_id):
        retval = vaultReply(self.vault.group_del_service(self.authtok, group_id,
                                                 service_id),
                            "Error removing service from group")

        print "Success: %s" % retval['message']

    @authenticate(True)
    def group_add_user(self, group_id, user, is_admin=False, retval=None):
        retval = vaultReply(self.vault.group_add_user(self.authtok, group_id,
                                                      user),
                            "Error adding user to group")

        # Decrypt cryptgroupkey
        # TODO: make use of a cache
        grouppacked = decrypt_longmsg(self.privkey, retval['cryptgroupkey'])
        
        # Get userpubkey and unpack
        eg = ElGamal.ElGamalobj()
        (eg.p, eg.g, eg.y) = unserial_elgamal_pubkey(retval['userpubkey'])
        
        # Re-encrypt for user
        newcryptgroupkey = encrypt_longmsg(eg, grouppacked)
        
        # Return a well-formed database-ready cryptgroupkey for user,
        # also, give the param is_admin.. as desired.
        retval = vaultReply(self.vault.group_add_user(self.authtok, group_id,
                                                      user, is_admin,
                                                      newcryptgroupkey),
                            "Error adding user to group")

        print "Success: %s" % retval['message']

    @authenticate()
    def group_del_user(self, group_id, user):
        retval = vaultReply(self.vault.group_del_user(self.authtok, group_id,
                                                      user),
                            "Error removing user from group")

        print "Success: %s" % retval['message']
    
    @authenticate()
    def group_add(self, group_name):
        """Add a named group to the Vault. Return the group id."""
        
        print "Please wait, Vault generating keypair..."
        
        retval = vaultReply(self.vault.group_add(self.authtok, group_name),
                            "Error adding group")

        print "Success: %s " % retval['message']
        print "New group id: g#%d" % retval['group_id']


    @authenticate()
    def group_del(self, group_id):
        """Remove a group from the Vault, making sure no services are left
        behind."""
        retval = vaultReply(self.vault.group_del(self.authtok, group_id),
                            "Error removing group")

        print "Success: %s" % retval['message']

    @authenticate()
    def group_list(self):
        """Simply list the available groups"""
        retval = vaultReply(self.vault.group_list(self.authtok),
                            "Error listing groups")

        print "Groups:"

        for grp in retval['list']:
            add = []
            if grp.get('hidden', False):
                add.append('[hidden]')
            if grp.get('member', False):
                add.append('[member]')
            if grp.get('admin', False):
                add.append('[admin]')
            print "\tg#%d\t%s %s" % (grp['id'], grp['name'], ' '.join(add))


    @authenticate()
    def machine_list(self, verbose=False, customer_id=None):
        retval = vaultReply(self.vault.machine_list(self.authtok, customer_id),
                            "Error listing machines")

        print "Machines list:"

        oldcid = 0
        for x in retval['list']:
            if oldcid != x['customer_id']:
                print "%s (c#%d)" % (x['customer_name'], x['customer_id'])
                oldcid = x['customer_id']
            print "\tm#%d\t%s (%s)" % (x['id'], x['name'], x['fqdn'] or x['ip'])
            if verbose:
                print "\t\tLocation: %s" % x['location'].replace('\n', '\t\t\n')
                print "\t\tNotes: %s" % x['notes'].replace('\n', '\t\t\n')
                print '-' * 76


    @authenticate()
    def customer_list(self, customer_id=None):
        retval = vaultReply(self.vault.customer_list(self.authtok),
                            "Error listing customers")

        # Receive a list: [{'id': '%d',
        #                   'name': 'blah'},
        #                  {'id': '%d',
        #                   'name': 'blah2'}]
        print "Customer list:"
        for x in retval['list']:
            print "c#%d\t%s" % (x['id'], x['name'])

