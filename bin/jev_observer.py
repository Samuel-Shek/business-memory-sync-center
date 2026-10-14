"""Load the shared implementation without modifying global sys.path."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import importlib.util,sys,os
from pathlib import Path
_LOCK=None

def observe(caller,task,state,**kwargs):
 if os.environ.get('JEV_DISABLE')=='1' or 'unittest' in sys.modules or 'pytest' in sys.modules:return False
 try:
  name='_example_jev_client'
  module=sys.modules.get(name)
  if module is None:
   root=Path(_archive_path('/Users/example/Code/active/jev-judgment-service/jev'))
   spec=importlib.util.spec_from_file_location(name,root/'__init__.py',submodule_search_locations=[str(root)])
   module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
  return importlib.import_module(name+'.client').observe(caller,task,state,**kwargs)
 except Exception:return False
def observe_batch(caller,observations,**kwargs):
 if os.environ.get('JEV_DISABLE')=='1' or 'unittest' in sys.modules or 'pytest' in sys.modules:return False
 try:
  name='_example_jev_client'
  module=sys.modules.get(name)
  if module is None:
   root=Path(_archive_path('/Users/example/Code/active/jev-judgment-service/jev'))
   spec=importlib.util.spec_from_file_location(name,root/'__init__.py',submodule_search_locations=[str(root)])
   module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
  return importlib.import_module(name+'.client').observe_batch(caller,observations,**kwargs)
 except Exception:return False
