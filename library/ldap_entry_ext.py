#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2016, Peter Sagerson <psagers@ignorare.net>
# Copyright: (c) 2016, Jiri Tyr <jiri.tyr@gmail.com>
#
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type


ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'community'
}


DOCUMENTATION = """
---
module: ldap_entry_ext
short_description: Add or remove LDAP entries.
description:
  - Add or remove LDAP entries. This module only asserts the existence or
    non-existence of an LDAP entry, not its attributes. To assert the
    attribute values of an entry, see M(ldap_attr).
notes:
  - The default authentication settings will attempt to use a SASL EXTERNAL
    bind over a UNIX domain socket. This works well with the default Ubuntu
    install for example, which includes a cn=peercred,cn=external,cn=auth ACL
    rule allowing root to modify the server configuration. If you need to use
    a simple bind to access your server, pass the credentials in I(bind_dn)
    and I(bind_pw).
version_added: '2.3'
author:
  - Jiri Tyr (@jtyr)
requirements:
  - python-ldap
options:
  attributes:
    description:
      - If I(state=present), attributes necessary to create an entry. Existing
        entries are never modified. To assert specific attribute values on an
        existing entry, use M(ldap_attr) module instead.
  objectClass:
    description:
      - If I(state=present), value or list of values to use when creating
        the entry. It can either be a string or an actual list of
        strings.
  params:
    description:
      - List of options which allows to overwrite any of the task or the
        I(attributes) options. To remove an option, set the value of the option
        to C(null).
  state:
    description:
      - The target state of the entry.
    choices: [present, absent]
    default: present
  indexed:
    default: 'no'
    type: bool
    description:
      - If true, index in leftmost dn component is ignored when testing for
        existing entries.
  indexed_before:
    description:
      - List of leftmost rdn values that this entry is to placed before.
  indexed_after:
    description:
      - List of leftmost rdn values that this entry is to placed after.
extends_documentation_fragment: ldap.documentation
"""


EXAMPLES = """
- name: Make sure we have a parent entry for users
  ldap_entry:
    dn: ou=users,dc=example,dc=com
    objectClass: organizationalUnit

- name: Make sure we have an admin user
  ldap_entry:
    dn: cn=admin,dc=example,dc=com
    objectClass:
      - simpleSecurityObject
      - organizationalRole
    attributes:
      description: An LDAP administrator
      userPassword: "{SSHA}tabyipcHzhwESzRaGA7oQ/SDoBZQOGND"

- name: Get rid of an old entry
  ldap_entry:
    dn: ou=stuff,dc=example,dc=com
    state: absent
    server_uri: ldap://localhost/
    bind_dn: cn=admin,dc=example,dc=com
    bind_pw: password

#
# The same as in the previous example but with the authentication details
# stored in the ldap_auth variable:
#
# ldap_auth:
#   server_uri: ldap://localhost/
#   bind_dn: cn=admin,dc=example,dc=com
#   bind_pw: password
- name: Get rid of an old entry
  ldap_entry:
    dn: ou=stuff,dc=example,dc=com
    state: absent
    params: "{{ ldap_auth }}"
"""


RETURN = """
# Default return values
"""

import re
import traceback

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.six import string_types
from ansible.module_utils._text import to_native, to_bytes
from ansible.module_utils.ldap import LdapGeneric, gen_specs

try:
    import ldap.modlist

    HAS_LDAP = True
except ImportError:
    HAS_LDAP = False


def _split_dn(dn):
    edn = ldap.dn.explode_dn(dn)
    return edn[0], ','.join(edn[1:])


def _split_rdn(rdn):
    rdn_attribute, rdn_value, _ = ldap.dn.str2dn(rdn)[0][0]
    return rdn_attribute, rdn_value


def _join_rdn(attribute, value):
    return '{}={}'.format(attribute, value)


class LdapEntry(LdapGeneric):
    def __init__(self, module):
        LdapGeneric.__init__(self, module)

        # Shortcuts
        self.state = self.module.params['state']

        # Add the objectClass into the list of attributes
        self.module.params['attributes']['objectClass'] = (
            self.module.params['objectClass'])

        # Load attributes
        if self.state == 'present':
            self.attrs = self._load_attrs()

    def _load_attrs(self):
        """ Turn attribute's value to array. """
        attrs = {}

        for name, value in self.module.params['attributes'].items():
            if name not in attrs:
                attrs[name] = []

            if isinstance(value, list):
                attrs[name] = list(map(to_bytes, value))
            else:
                attrs[name].append(to_bytes(value))

        return attrs

    def add(self):
        """ If self.dn does not exist, returns a callable that will add it. """
        def _add():
            self.connection.add_s(self.dn, modlist)

        def _rename():
            self.connection.rename_s(self.dn, newrdn)

        if not self._has_index_constraints():
            if not self._is_entry_present():
                modlist = ldap.modlist.addModlist(self.attrs)
                action = _add
            else:
                action = None
        else:
            # Evaluate index constraints
            self_index, min_index, max_index = self._get_indexes()
            is_present = self_index is not None

            if is_present and self_index >= min_index and (max_index is None or self_index <= max_index):
                # Entry is present and index constraints are satisfied
                action = None
            else:
                # Calculate optimal index
                if max_index is not None:
                    if min_index > max_index:
                        self.module.fail_json(
                            msg='Satisfying index constraints would require reordering of sibling entries.')
                    index = max_index
                else:
                    index = None

                # Derive indexed rdn
                rdn, parent = _split_dn(self.dn)
                if index is not None:
                    rdn_attribute, rdn_value = _split_rdn(rdn)
                    rdn = _join_rdn(rdn_attribute, '{{{index}}}{value}'.format(index=index, value=rdn_value))

                if not is_present:
                    self.dn = '{},{}'.format(rdn, parent)
                    modlist = ldap.modlist.addModlist(self.attrs)
                    action = _add
                else:
                    newrdn = rdn
                    action = _rename

        return action

    def delete(self):
        """ If self.dn exists, returns a callable that will delete it. """
        def _delete():
            self.connection.delete_s(self.dn)

        if self._is_entry_present():
            action = _delete
        else:
            action = None

        return action

    def _is_entry_present(self):
        try:
            if not self.module.params['indexed']:
                self.connection.search_s(self.dn, ldap.SCOPE_BASE)
            else:
                edn = ldap.dn.explode_dn(self.dn)
                rdn, parent = _split_dn(self.dn)
                if not self.connection.search_s(parent, ldap.SCOPE_ONELEVEL, '({})'.format(rdn)):
                    raise ldap.NO_SUCH_OBJECT
        except ldap.NO_SUCH_OBJECT:
            is_present = False
        else:
            is_present = True

        return is_present

    def _has_index_constraints(self):
        return self.module.params['indexed_before'] or self.module.params['indexed_after']

    def _get_indexes(self):
        indexed_before = self.module.params['indexed_before']
        indexed_after = self.module.params['indexed_after']

        rdn, parent = _split_dn(self.dn)
        rdn_attribute, rdn_value = _split_rdn(rdn)
        siblings = self.connection.search_s(parent, ldap.SCOPE_ONELEVEL, '({}=*)'.format(rdn_attribute))
        siblings_indexes = {}
        for sibling in siblings:
            sibling_dn = sibling[0]
            _, sibling_rdn_value = _split_rdn(sibling_dn)
            match = re.match(r'\{(?P<index>\d+)\}(?P<value>.+)', sibling_rdn_value)
            if not match:
                self.module.fail_json(msg='Sibling entry {} does not have an indexed rdn.'.format(sibling_dn))
            siblings_indexes[match.group('value')] = int(match.group('index'))

        present_indexed_before = [siblings_indexes[e] for e in indexed_before if e in siblings_indexes]
        max_index = max(present_indexed_before) if present_indexed_before else None

        present_indexed_after = [siblings_indexes[e] for e in indexed_after if e in siblings_indexes]
        min_index = min(present_indexed_after) + 1 if present_indexed_after else 0

        self_index = siblings_indexes.get(rdn_value, None)

        return self_index, min_index, max_index

def main():
    module = AnsibleModule(
        argument_spec=gen_specs(
            attributes=dict(default={}, type='dict'),
            objectClass=dict(type='raw'),
            params=dict(type='dict'),
            state=dict(default='present', choices=['present', 'absent']),
            indexed=dict(default=False, type='bool'),
            indexed_before=dict(default=[], type='list'),
            indexed_after=dict(default=[], type='list'),
        ),
        supports_check_mode=True,
    )

    if not HAS_LDAP:
        module.fail_json(
            msg="Missing required 'ldap' module (pip install python-ldap).")

    state = module.params['state']

    # Check if objectClass is present when needed
    if state == 'present' and module.params['objectClass'] is None:
        module.fail_json(msg="At least one objectClass must be provided.")

    # Check if objectClass is of the correct type
    if (
            module.params['objectClass'] is not None and not (
                isinstance(module.params['objectClass'], string_types) or
                isinstance(module.params['objectClass'], list))):
        module.fail_json(msg="objectClass must be either a string or a list.")

    # Update module parameters with user's parameters if defined
    if 'params' in module.params and isinstance(module.params['params'], dict):
        for key, val in module.params['params'].items():
            if key in module.argument_spec:
                module.params[key] = val
            else:
                module.params['attributes'][key] = val

        # Remove the params
        module.params.pop('params', None)

    # Instantiate the LdapEntry object
    ldap = LdapEntry(module)

    # Get the action function
    if state == 'present':
        action = ldap.add()
    elif state == 'absent':
        action = ldap.delete()

    # Perform the action
    if action is not None and not module.check_mode:
        try:
            action()
        except Exception as e:
            module.fail_json(msg="Entry action failed.", details=to_native(e), exception=traceback.format_exc())

    module.exit_json(changed=(action is not None))


if __name__ == '__main__':
    main()
