import asyncio
import unittest

from mcp_server.oic_client import OICClient


class OICProjectMonitoringTests(unittest.TestCase):
    def test_project_instances_uses_project_monitoring_path(self):
        client = OICClient.__new__(OICClient)
        calls = []

        async def fake_get(path, params=None, include_instance=True):
            calls.append((path, params, include_instance))
            return {"items": [{"id": "i-1"}]}

        client._get = fake_get
        client._build_q_filter = lambda **kwargs: "{code : 'X', timewindow : '1h', projectCode : 'proj-1'}"

        async def runner():
            return await client.list_project_monitoring_instances("proj-1", integration_id="X", timewindow="1h")

        payload = asyncio.run(runner())

        self.assertEqual(payload["items"][0]["id"], "i-1")
        self.assertEqual(calls[0][0], "/ic/api/integration/v1/monitoring/instances")
        self.assertEqual(calls[0][1]["q"], "{code : 'X', timewindow : '1h', projectCode : 'proj-1'}")
        self.assertTrue(calls[0][2])

    def test_project_message_summary_uses_project_path(self):
        client = OICClient.__new__(OICClient)
        calls = []

        async def fake_get(path, params=None, include_instance=True):
            calls.append((path, params, include_instance))
            return {"summary": {"total": 5}}

        client._get = fake_get
        client._build_q_filter = lambda **kwargs: "{code : 'X', projectCode : 'proj-1'}"

        async def runner():
            return await client.get_project_message_count_summary("proj-1", integration_id="X")

        payload = asyncio.run(runner())

        self.assertEqual(payload["summary"]["total"], 5)
        self.assertEqual(calls[0][0], "/ic/api/integration/v1/monitoring/integrations/messages/summary")
        self.assertEqual(calls[0][1]["q"], "{code : 'X', projectCode : 'proj-1'}")
        self.assertTrue(calls[0][2])

    def test_instance_activity_stream_details_supports_timezone_and_instance(self):
        client = OICClient.__new__(OICClient)
        calls = []

        async def fake_get(path, params=None):
            calls.append((path, params))
            return {"status": "ok"}

        client._get = fake_get

        async def runner():
            return await client.get_instance_activity_stream_details(
                "5TCtqLG3EfGKSQ_xnFop9g",
                timezone="Asia/Calcutta",
                integration_instance="ap-ccswacs-oic-dev-axhknnpmgslz-si",
            )

        payload = asyncio.run(runner())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(calls[0][0], "/ic/api/integration/v1/monitoring/instances/5TCtqLG3EfGKSQ_xnFop9g/activityStreamDetails")
        self.assertEqual(calls[0][1]["timezone"], "Asia/Calcutta")
        self.assertEqual(calls[0][1]["integrationInstance"], "ap-ccswacs-oic-dev-axhknnpmgslz-si")

    def test_multiple_integration_instances_are_preserved_as_repeated_query_params(self):
        client = OICClient.__new__(OICClient)
        original_settings = __import__("mcp_server.settings", fromlist=["settings"]).settings
        original_instance_name = original_settings.oic_instance_name
        original_settings.oic_instance_name = "ap-ccswacs-oic-dev-axhknnpmgslz-si"

        try:
            params = {
                "q": "{projectId : 'APDU_WACS_DXI_GIS', timewindow : 'RETENTIONPERIOD'}",
                "integrationInstance": [
                    "ap-ccswacs-oic-dev-axhknnpmgslz-si",
                    "ap-ccs-oic-uat-axhknnpmgslz-si",
                ],
            }

            result = client._with_instance_param(params)

            self.assertEqual(result["q"], "{projectId : 'APDU_WACS_DXI_GIS', timewindow : 'RETENTIONPERIOD'}")
            self.assertEqual(result["integrationInstance"], [
                "ap-ccswacs-oic-dev-axhknnpmgslz-si",
                "ap-ccs-oic-uat-axhknnpmgslz-si",
            ])
        finally:
            original_settings.oic_instance_name = original_instance_name

    def test_project_monitoring_requests_include_default_instance_for_tenant_monitoring(self):
        client = OICClient.__new__(OICClient)
        original_settings = __import__("mcp_server.settings", fromlist=["settings"]).settings
        original_instance_name = original_settings.oic_instance_name
        original_settings.oic_instance_name = "ap-ccswacs-oic-dev-axhknnpmgslz-si"

        try:
            params = {"q": "{projectCode : 'APDU_WACS_DXI_GIS', timewindow : 'RETENTIONPERIOD'}"}
            result = client._with_instance_param(params)
            self.assertEqual(result["q"], "{projectCode : 'APDU_WACS_DXI_GIS', timewindow : 'RETENTIONPERIOD'}")
            self.assertEqual(result["integrationInstance"], "ap-ccswacs-oic-dev-axhknnpmgslz-si")
        finally:
            original_settings.oic_instance_name = original_instance_name

    def test_project_integration_detail_uses_project_path_with_instance(self):
        client = OICClient.__new__(OICClient)
        calls = []

        async def fake_get(path, params=None, include_instance=True):
            calls.append((path, params, include_instance))
            return {"code": "APDU_DXI_OFSC_COSTESTI", "version": "01.00.0021", "status": "ACTIVE"}

        client._get = fake_get

        async def runner():
            return await client.get_project_integration(
                "APDU_WACS_DXI_GIS",
                "APDU_DXI_OFSC_COSTESTI|01.00.0021",
            )

        payload = asyncio.run(runner())

        self.assertEqual(payload["code"], "APDU_DXI_OFSC_COSTESTI")
        self.assertEqual(calls[0][0], "/ic/api/integration/v1/projects/APDU_WACS_DXI_GIS/integrations/APDU_DXI_OFSC_COSTESTI%7C01.00.0021")
        self.assertTrue(calls[0][2])


if __name__ == "__main__":
    unittest.main()
