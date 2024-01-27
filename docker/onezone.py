#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import subprocess as sp
import sys
import time

import textwrap
import yaml
import requests
from requests import codes
from requests.packages.urllib3.exceptions import InsecureRequestWarning

try:
    import xml.etree.cElementTree as eTree
except ImportError:
    import xml.etree.ElementTree as eTree

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


COMPONENTS = ['cluster_manager', 'oz_panel', 'oz_worker']

LOGS = [('[oz_panel]', '/var/log/oz_panel'),
        ('[cluster_manager]', '/var/log/cluster_manager'),
        ('[oz_worker]', '/var/log/oz_worker')]
LOG_LEVELS = ['debug', 'info', 'error']

# If onepanel is started from sources, contains the path to the root directory
# of the pre-built onepanel repo/artifact.
ONEPANEL_OVERRIDE = 'ONEPANEL_OVERRIDE'

# Username to be used in Basic auth with the emergency passphrase
PASSPHRASE_USERNAME = 'onepanel'
EMERGENCY_PASSPHRASE_VARIABLE = 'ONEPANEL_EMERGENCY_PASSPHRASE'

LEGACY_VM_ARGS_SOURCES_PATH_TEMPLATE = '_build/default/rel/{component}/etc/vm.args'

ONP_GENERATED_CONFIG_SOURCES_PATH = '_build/default/rel/oz_panel/etc/autogenerated.config'
ONP_VM_ARGS_SOURCES_PATH = '_build/default/rel/oz_panel/etc/autogenerated.vm.args'

LEGACY_VM_ARGS_PACKAGES_PATH_TEMPLATE = '/etc/{component}/vm.args'

ONP_GENERATED_CONFIG_PACKAGES_PATH = '/etc/oz_panel/autogenerated.config'
ONP_VM_ARGS_PACKAGES_PATH = '/etc/oz_panel/autogenerated.vm.args'

AUTOGENERATED_VM_ARGS_TEMPLATE = '''# MACHINE GENERATED FILE. DO NOT MODIFY.

## Name of the node
-name {node}

## Cookie for distributed erlang
-setcookie {cookie}
'''

WRAP_KWARGS = {
    'width': 100,
    'break_long_words': False,
    'break_on_hyphens': False
}

SERVICE_READY_LOCK_FILE = '/root/service-ready.lock'  # used by the "await" script


class MissingVariableError(Exception):
    """Indicates operation error caused by missing environment variable"""
    def __init__(self, variable_name):
        message = "Missing {} environment variable".format(variable_name)
        super(Exception, self).__init__(message)
        self.variable_name = variable_name


class AuthenticationError(Exception):
    """Indicates failure to execute an API request caused by rejected authentication"""
    pass


def log(message, end='\n'):
    sys.stdout.write(message + end)
    sys.stdout.flush()


def replace(file_path, pattern, value):
    with open(file_path, 'r+') as f:
        content = f.read()
        content = re.sub(pattern, value, content)
        f.seek(0)
        f.truncate()
        f.write(content)


def set_node_name(file_path):
    hostname = sp.check_output(['hostname', '-f']).decode().rstrip('\n')
    replace(file_path, r'-name .*', '-name onepanel@{0}'.format(hostname))


def config_file_initialized(file_path):
    try:
        with open(file_path, 'r') as f:
            content = f.read()
            return bool(re.search(r'{config_initialized,\s*true}', content))
    except:
        return False


def generate_config_file(file_path):
    content = ('% MACHINE GENERATED FILE. DO NOT MODIFY.\n'
               '% Use overlay.config for custom configuration.\n\n'
               '[{onepanel, [{config_initialized, true}')

    generate_test_web_cert = os.environ.get('ONEPANEL_GENERATE_TEST_WEB_CERT')
    if generate_test_web_cert:
        domain = os.environ.get('ONEPANEL_GENERATED_CERT_DOMAIN')
        flag = 'true' if generate_test_web_cert == 'true' else 'false'
        content += ',\n{{generate_test_web_cert, {0}}}'.format(flag)
        content += ',\n{{test_web_cert_domain, "{0}"}}'.format(domain)

    trust_test_ca = os.environ.get('ONEPANEL_TRUST_TEST_CA')
    if trust_test_ca:
        flag = 'true' if trust_test_ca == 'true' else 'false'
        content += ',\n{{treat_test_ca_as_trusted, {0}}}'.format(flag)

    content += '\n]}].'

    with open(file_path, 'w') as f:
        f.write(content)


def start_onepanel():
    log('Starting oz_panel...')

    cmd = ['service', 'oz_panel', 'start']
    if os.environ.get(ONEPANEL_OVERRIDE):
        oz_panel_script = os.path.join(
            os.environ.get(ONEPANEL_OVERRIDE),
            "_build/default/rel/oz_panel/bin/oz_panel"
        )
        cmd = [oz_panel_script, 'start']

    try:
        sp.check_output(cmd)
    except sp.CalledProcessError as err:
        log('ERROR: ' + str(err))
        log('Output was:\n')
        log(str(err.output))
        sys.exit(err.returncode)

    wait_for_rest_listener()
    log('[  OK  ] oz_panel started\n')


def wait_for_rest_listener():
    first = True
    connected = False
    while not connected:
        try:
            requests.get('https://127.0.0.1:9443/api/v3/onepanel/', verify=False)
        except requests.ConnectionError:
            if first:
                log('Waiting for oz_panel server to be available\n'
                    '(may require starting other cluster nodes)\n')
                first = False
            time.sleep(1)
        else:
            connected = True


def format_step(step):
    service, action = step.split(':')
    return '* {0}: {1}'.format(service, action)


def get_emergency_passphrase():
    """Returns emergency passphrase provided in environment variable"""
    passphrase = os.environ.get(EMERGENCY_PASSPHRASE_VARIABLE)
    if passphrase is None:
        raise MissingVariableError(EMERGENCY_PASSPHRASE_VARIABLE)
    return passphrase


def set_emergency_passphrase(passphrase):
    r = requests.put('https://127.0.0.1:9443/api/v3/onepanel/emergency_passphrase',
                     headers={'content-type': 'application/json'},
                     data=json.dumps({'newPassphrase': passphrase}),
                     verify=False)
    if r.status_code in (codes.unauthorized, codes.forbidden):
        raise AuthenticationError('Could not set Onepanel emergency passphrase due to '
                                  'authentication error: {} {}'
                                  .format(r.status_code, r.text))
    if not r.ok:
        raise RuntimeError('Could not set Onepanel emergency passphrase: {} {}'
                           .format(r.status_code, r.text))


def do_auth_request(request, *args, **kwargs):
    passphrase = get_emergency_passphrase()
    r = request(*args, auth=(PASSPHRASE_USERNAME, passphrase), **kwargs)
    if r.status_code in (codes.unauthorized, codes.forbidden):
        raise AuthenticationError('Authentication error.\n'
                                  'Please ensure that valid emergency passphrase is present\n'
                                  'in the {} environment variable'
                                  .format(EMERGENCY_PASSPHRASE_VARIABLE))
    return r


def get_batch_config():
    batch_config = os.environ.get('ONEZONE_CONFIG', '')
    batch_config = yaml.safe_load(batch_config)
    if not batch_config:
        return {}

    # insert interactiveDeployment mark if not present
    onepanel_config = batch_config.get('onepanel', {})
    if 'interactiveDeployment' not in onepanel_config:
        onepanel_config['interactiveDeployment'] = False
        batch_config['onepanel'] = onepanel_config

    return batch_config


def configure(config):
    """
    Attempts to deploy the Onezone
    :return: False if configuration was skipped because of existing deployment,
        True upon success
    """
    passphrase = get_emergency_passphrase()

    try:
        set_emergency_passphrase(passphrase)
    except AuthenticationError:
        # emergency passphrase setup failure indicates existing deployment
        return False

    r = do_auth_request(requests.post,
                        'https://127.0.0.1:9443/api/v3/onepanel/zone/configuration',
                        headers={'content-type': 'application/x-yaml'},
                        data=yaml.dump(config),
                        verify=False)

    if r.status_code == codes.conflict:
        return False

    if not r.ok:
        raise RuntimeError(
            'Failed to start configuration process, the response was:\n'
            '  code: {0}\n'
            '  body: {1}\n'
            'For more information please check the logs.'
            .format(r.status_code, r.text))

    loc = r.headers['location']
    status = 'running'
    steps = []
    resp = {}

    log('\nConfiguring Onezone:')
    while status == 'running':
        r = do_auth_request(requests.get,
                            'https://127.0.0.1:9443' + loc,
                            verify=False)
        if not r.ok:
            raise RuntimeError('Unexpected configuration error\n{0}'
                               'For more information please check the logs.'
                               .format(r.text))
        else:
            resp = json.loads(r.text)
            status = resp.get('status', 'error')
            for step in resp.get('steps', []):
                if steps and step == steps[0]:
                    steps = steps[1:]
                else:
                    log(format_step(step))
            steps = resp.get('steps', [])
            time.sleep(1)

    if status != 'ok':
        raise RuntimeError(format_error(resp))
    return True


def format_error(response):
    if 'error' not in response:
        return "Error: unexpected server response"
    error_obj = response['error']
    error_id, descr, details, nodes = unwrap_error_with_hosts(error_obj)
    details_str = format_dict(details, '    ')

    if details_str:
        details_str = '  details:\n' + details_str
    nodes_str = ''
    if nodes:
        nodes_str = '  nodes:\n    ' + '\n    '.join(sorted(nodes))
    descr_str = textwrap.fill('description: ' + descr, initial_indent='  ',
                              subsequent_indent='    ', **WRAP_KWARGS)
    return "Error:\n  id: {}\n{}\n{}\n{}".format(
        error_id, nodes_str, descr_str, details_str)


def format_dict(details, indent):
    result = ''
    for key, value in list(details.items()):
        if isinstance(value, dict):
            result += indent + '{}:\n'.format(key)
            result += format_dict(value, indent + '  ')
        else:
            result += textwrap.fill('{}: {}'.format(key, value),
                                    initial_indent=indent,
                                    subsequent_indent=indent + '  ',
                                    **WRAP_KWARGS) + '\n'
    return result


def unwrap_error_with_hosts(error_obj):
    """Extracts original error from a wrapper listing hosts
    where the error occurred"""
    error_id = error_obj.get('id')
    description = error_obj.get('description')
    details = error_obj.get('details', {})
    nodes = []

    if error_id == 'errorOnNodes' and 'error' in details:
        error_id = details['error']['id']
        description = details['error']['description']
        nodes = details['hostnames']
        details = details['error'].get('details', {})

    return error_id, description, details, nodes


def wait_for_workers():
    url = 'https://127.0.0.1:9443/api/v3/onepanel/zone/nagios'
    retries = 0
    while not nagios_up(url, retries % 10 == 9):
        time.sleep(1)
        retries += 1
        if retries == 120:
            raise RuntimeError('Timeout waiting for the Onepanel service readiness')


def nagios_up(url, log_errors=False):
    try:
        r = do_auth_request(requests.get, url, verify=False)
        if not r.ok:
            if log_errors:
                log('Onepanel service is reachable, but nagios is not reporting an OK status - retrying...')
            return False
    except Exception:
        if log_errors:
            log('Onepanel service is not reachable - retrying...')
        return False

    healthdata = eTree.fromstring(r.text)
    return healthdata.attrib['status'] == 'ok'


def get_container_id():
    with open('/proc/self/cgroup', 'r') as f:
        return f.readline().split('/')[-1].rstrip('\n')


def inspect_container(container_id):
    try:
        result = sp.check_output([
            'curl', '-s', '--unix-socket',
            '/var/run/docker.sock',
            'http:/containers/{0}/json'.format(container_id)
        ])
        return json.loads(result)
    except Exception:
        return {}


def show_ip_address(json):
    ip = '-'
    try:
        ip = sp.check_output(['hostname', '-i']).rstrip('\n')
        ip = list(json['NetworkSettings']['Networks'].items())[0][1]['IPAddress']
    except Exception:
        pass
    log('* IP Address: {0}'.format(ip))


def show_ports(json):
    ports = json.get('NetworkSettings', {}).get('Ports', {})
    ports_format = []
    for container_port in ports:
        host = ports[container_port]
        if host:
            for host_port in host:
                ports_format.append('{0}:{1} -> {2}'.format(
                    host_port['HostIp'], host_port['HostPort'], container_port
                ))
        else:
            ports_format.append(container_port)
    ports_str = '\n         '.join(ports_format) if ports_format else '-'
    log('* Ports: {0}'.format(ports_str))


def show_details():
    log('\nContainer details:')

    container_id = get_container_id()
    json = inspect_container(container_id)

    show_ip_address(json)
    show_ports(json)


def print_logs(log_level, starting_log_cursors=None, infinitely=True):
    if log_level in LOG_LEVELS:
        log('\nLogging on \'{0}\' level:'.format(log_level))
        if not starting_log_cursors:
            starting_log_cursors = []
            for log_prefix, log_dir in LOGS:
                log_file = os.path.join(log_dir, log_level + '.log')
                starting_log_cursors.append((log_prefix, log_file, None, None))

    new_log_cursors = print_new_logs(starting_log_cursors)
    while infinitely:
        new_log_cursors = print_new_logs(new_log_cursors)
        time.sleep(1)
    return new_log_cursors


def print_new_logs(log_cursors):
    new_logs = []

    for log_prefix, log_file, log_fd, log_ino in log_cursors:
        try:
            if os.stat(log_file).st_ino != log_ino:
                if log_fd:
                    log_fd.close()
                log_fd = open(log_file, 'r')
                log_ino = os.stat(log_file).st_ino

            log_line = log_fd.readline()
            while log_line:
                log('{0} {1}'.format(log_prefix, log_line), end='')
                log_line = log_fd.readline()

            new_logs.append((log_prefix, log_file, log_fd, log_ino))
        except:
            new_logs.append((log_prefix, log_file, None, None))

    return new_logs


def upgrade_legacy_vm_args_if_exists():
    if os.environ.get(ONEPANEL_OVERRIDE):
        legacy_vm_args_path_template = os.path.join(
            os.environ.get(ONEPANEL_OVERRIDE), 
            '../{component}',
            LEGACY_VM_ARGS_SOURCES_PATH_TEMPLATE
        )
    else:
        legacy_vm_args_path_template = LEGACY_VM_ARGS_PACKAGES_PATH_TEMPLATE

    for component in COMPONENTS:
        legacy_vm_args_path = legacy_vm_args_path_template.format(component=component)

        if os.path.exists(legacy_vm_args_path):
            upgrade_legacy_vm_args(legacy_vm_args_path)


def upgrade_legacy_vm_args(legacy_vm_args_path):
    with open(legacy_vm_args_path, 'r+') as f:
        content = f.read()
        node = re.search(r'-name (?P<node>.*)', content).group('node')
        cookie = re.search(r'-setcookie (?P<cookie>.*)', content).group('cookie')

    autogenerated_vm_args_path = os.path.join(os.path.dirname(legacy_vm_args_path), 
                                              'autogenerated.vm.args')

    with open(autogenerated_vm_args_path, 'w+') as f:
        f.write(AUTOGENERATED_VM_ARGS_TEMPLATE.format(node=node, cookie=cookie))

    os.remove(legacy_vm_args_path)

    log(f'Upgraded legacy vm.args: {legacy_vm_args_path} -> {autogenerated_vm_args_path}')


if __name__ == '__main__':
    current_log_level = os.environ.get('ONEPANEL_LOG_LEVEL', 'info').lower()
    try:
        upgrade_legacy_vm_args_if_exists()

        if os.environ.get(ONEPANEL_OVERRIDE):
            app_config_path = os.path.join(os.environ.get(ONEPANEL_OVERRIDE),
                                           ONP_GENERATED_CONFIG_SOURCES_PATH)

            vm_args_path = os.path.join(os.environ.get(ONEPANEL_OVERRIDE),
                                        ONP_VM_ARGS_SOURCES_PATH)
        else:
            app_config_path = ONP_GENERATED_CONFIG_PACKAGES_PATH
            vm_args_path = ONP_VM_ARGS_PACKAGES_PATH

        config_file_existed = config_file_initialized(app_config_path)
        if not config_file_existed:
            generate_config_file(app_config_path)

        set_node_name(vm_args_path)
        start_onepanel()

        batch_mode = os.environ.get('ONEPANEL_BATCH_MODE', 'false')
        if batch_mode.lower() == 'true':
            batch_config = get_batch_config()
            try:
                if configure(batch_config):
                    log('\nCongratulations! New Onezone deployment successfully started.')
                else:
                    log("\nWaiting for existing cluster to start...")
                    wait_for_workers()
                    log('Existing Onezone deployment resumed work.')
            except AuthenticationError as e:
                log('The launch script cannot access onepanel to manage the deployment process.\n'
                    'Please ensure that valid emergency passphrase is present\n'
                    'in the {} environment variable\n'
                    'or oversee the cluster status manually.'.format(EMERGENCY_PASSPHRASE_VARIABLE))
            except MissingVariableError as e:
                if e.variable_name != EMERGENCY_PASSPHRASE_VARIABLE:
                    raise
                if config_file_existed:
                    # most likely this is an existing deployment
                    log('Environment variable {} is missing.\n'
                        'An existing deployment will continue to work\n'
                        'but this script will not be able to track its startup.\n'
                        'You can fix this by setting the variable\n'
                        'to the Onepanel\' passphrase (formerly admin account\'s password).'
                        .format(EMERGENCY_PASSPHRASE_VARIABLE))
                else:
                    log('Environment variable {} is missing.\n'
                        'When deploying a new cluster this variable is required.\n'
                        'Please set it to your desired Onepanel emergency passphrase.\n'
                        .format(EMERGENCY_PASSPHRASE_VARIABLE))

        show_details()
    except Exception as e:
        log('\nERROR: {0}'.format(e))
        if os.environ.get('ONEPANEL_DEBUG_MODE', 'false').lower() == 'true':
            pass
        else:
            log(' ')
            log('Below is a dump of all application logs at the moment of failure:')
            log('-------------------------------------------------------------')
            print_logs(current_log_level, infinitely=False)
            log('-------------------------------------------------------------')
            log('ERROR: Starting the container failed - see above.')
            sys.exit(1)

    previous_log_cursors = print_logs(current_log_level, infinitely=False)
    with open(SERVICE_READY_LOCK_FILE, 'w'):
        pass
    print_logs(current_log_level, starting_log_cursors=previous_log_cursors, infinitely=True)
