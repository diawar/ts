from django.test import SimpleTestCase
from django.urls import reverse, resolve


class WorkspaceRouteTests(SimpleTestCase):
    def test_canonical_routes_use_workspace(self):
        self.assertEqual(reverse('workspace'), '/workspace/')
        self.assertEqual(reverse('collection_move',kwargs={'item_id':17}), '/workspace/items/17/stage/')
        self.assertEqual(resolve('/workspace/').url_name, 'workspace')

    def test_old_route_is_removed_without_redirect(self):
        response=self.client.get('/collections/?profile=all&stage=preparing&sort=-amount')
        self.assertEqual(response.status_code,404)
        self.assertNotIn('Location',response)

    def test_old_action_route_is_removed(self):
        response=self.client.post('/collections/items/17/stage/',{'stage':'preparing'})
        self.assertEqual(response.status_code,404)
