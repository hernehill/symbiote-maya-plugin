name = 'symbiote_maya_plugin'

version = '0.2.0.hh.1.0.0'

authors = [
    'symbiote',
]

description = '''maya plugin for symbiote'''

with scope('config') as c:
    import os
    c.release_packages_path = os.environ['HH_REZ_REPO_RELEASE_EXT']
    c.plugins.release_hook.hh_emailer.recipients = []

requires = [
    'maya'
]

private_build_requires = [
]

variants = [
]

def commands():
    env.REZ_SYMBIOTE_MAYA_PLUGIN_ROOT = '{root}'
    env.PATH.append('{root}/bin')
    env.PYTHONPATH.append('{root}/src/python')

build_command = 'rez python {root}/rez_build.py'

uuid = 'repository.symbiote-maya-plugin'
