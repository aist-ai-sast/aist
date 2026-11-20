"""The project list tells each user which projects they may change, as the project's own policy decides."""
from __future__ import annotations

from django.urls import reverse
from dojo.authorization.roles_permissions import Roles

from aist.test.test_api import AISTApiBase


class ProjectListCanOperateTests(AISTApiBase):

    def _listed(self, client) -> dict[int, bool]:
        resp = client.get(reverse("aist_api:project_list"))
        self.assertEqual(resp.status_code, 200, resp.data)
        rows = resp.data["results"] if isinstance(resp.data, dict) else resp.data
        return {row["id"]: row["can_operate"] for row in rows}

    def test_a_maintainer_may_operate_the_project(self):
        self.assertEqual(self._listed(self.client), {self.project.id: True})

    def test_a_reader_sees_the_project_but_may_not_operate_it(self):
        reader = self.client_for_member("project_reader", Roles.Reader)

        self.assertEqual(self._listed(reader), {self.project.id: False})

    def test_another_organization_s_project_is_not_listed_even_to_its_writer_role_elsewhere(self):
        other_maintainer = self.client_for_member("other_maintainer", Roles.Maintainer, product_type=self.other_prod_type)

        self.assertEqual(self._listed(other_maintainer), {self.other_project.id: True})
