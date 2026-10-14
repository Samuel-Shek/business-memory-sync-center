
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest
import media_actions as m
class Routing(unittest.TestCase):
 def card(self,**kw):return {'state':'pending','needs_confirmation':True,'display_name':'fixture',**kw}
 def test_video_standby_is_not_unanswered_choice(self):
  value={};m.route_value(value,[self.card()]);self.assertEqual(value['attention_state'],'waiting');self.assertEqual(value['attention_label'],'视频待命')
 def test_authorized_processing_remains_automatic(self):
  value={};m.route_value(value,[self.card(state='running')]);self.assertEqual(value['attention_state'],'automatic')
 def test_unknown_type_stays_visible(self):
  value={};m.route_value(value,[self.card(needs_confirmation=False,routing_wait=True)]);self.assertEqual(value['attention_state'],'waiting')
 def test_resolved_not_reopened(self):
  value={'attention_state':'closed'};m.route_value(value,[self.card(state='ignored')]);self.assertEqual(value['attention_state'],'closed')
if __name__=='__main__':unittest.main()
