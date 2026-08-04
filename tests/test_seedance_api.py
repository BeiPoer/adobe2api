import unittest
from unittest.mock import Mock

from api.routes.generation import (
    SEEDANCE_MODEL_ID,
    SEEDANCE_NEGATIVE_PROMPT,
    _parse_seedance_request,
)
from core.adobe_client import AdobeClient, UpstreamTemporaryError


class SeedanceApiTest(unittest.TestCase):
    def test_seedance_uses_firefly_headers_once_for_upstream_408(self):
        client = object.__new__(AdobeClient)
        client.api_key = "projectx_webapp"
        client.user_agent = "test-agent"
        client.sec_ch_ua = '"Chromium";v="143"'
        client.video_submit_url = "https://example.com/video"
        response = Mock(
            status_code=408,
            text='{"error_code":"timeout_error","message":"system under load"}',
            headers={},
        )
        client._post_json = Mock(return_value=response)

        with self.assertRaises(UpstreamTemporaryError) as raised:
            client.generate_video(
                token="token",
                video_conf={"engine": "seedance2", "resolution": "480p"},
                prompt="苹果变青色",
                aspect_ratio="16:9",
                duration=5,
            )

        self.assertEqual(raised.exception.status_code, 408)
        self.assertEqual(client._post_json.call_count, 1)
        headers = client._post_json.call_args.kwargs["headers"]
        self.assertEqual(headers["origin"], "https://firefly.adobe.com")
        self.assertEqual(headers["x-api-key"], "clio-playground-web")

    def test_seedance_reuses_legacy_video_submit_headers(self):
        client = object.__new__(AdobeClient)
        client.api_key = "projectx_webapp"
        client.user_agent = "test-agent"
        client.sec_ch_ua = '"Chromium";v="143"'
        client.upload_url = "https://example.com/upload"
        token = "token"
        response = Mock(status_code=200, text="")
        response.json.return_value = {"images": [{"id": "image-id"}]}

        with unittest.mock.patch.object(
            client, "_post_bytes", return_value=response
        ) as post_bytes:
            headers = client._video_submit_headers(token)
            image_id = client.upload_image(
                token, b"image", "image/jpeg", firefly=True
            )

        upload_headers = post_bytes.call_args.kwargs["headers"]

        self.assertEqual(image_id, "image-id")
        self.assertEqual(headers["x-api-key"], "projectx_webapp")
        self.assertEqual(headers["origin"], "https://new.express.adobe.com")
        self.assertEqual(headers["referer"], "https://new.express.adobe.com/")
        self.assertNotIn("x-arp-session-id", headers)
        self.assertNotIn("x-nonce", headers)
        self.assertEqual(upload_headers["x-api-key"], "clio-playground-web")
        self.assertEqual(upload_headers["origin"], "https://firefly.adobe.com")
        self.assertEqual(upload_headers["content-type"], "image/jpeg")
        self.assertNotIn("x-nonce", upload_headers)
        self.assertNotIn("x-arp-session-id", upload_headers)

    def test_seedance_reports_451_as_upstream_rejection(self):
        client = object.__new__(AdobeClient)
        client.user_agent = "test-agent"
        client.sec_ch_ua = '"Chromium";v="143"'
        client.video_submit_url = "https://example.com/video"
        response = Mock(
            status_code=451,
            text='{"error_code":"legal_error","message":"{}"}',
            headers={},
        )
        client._post_json = Mock(return_value=response)

        with self.assertRaises(UpstreamTemporaryError) as raised:
            client.generate_video(
                token="token",
                video_conf={"engine": "seedance2", "resolution": "480p"},
                prompt="苹果变青色",
                aspect_ratio="16:9",
                duration=5,
            )

        self.assertEqual(raised.exception.status_code, 451)
        self.assertEqual(raised.exception.error_type, "status")
        self.assertEqual(client._post_json.call_count, 1)

    def test_official_request_maps_to_captured_adobe_payload(self):
        request = _parse_seedance_request(
            {
                "model": SEEDANCE_MODEL_ID,
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/last.jpg"},
                        "role": "last_frame",
                    },
                    {"type": "text", "text": "摘下苹果"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/first.jpg"},
                        "role": "first_frame",
                    },
                ],
                "resolution": "480p",
                "ratio": "16:9",
                "duration": 5,
                "seed": 202381,
                "generate_audio": False,
            }
        )
        self.assertEqual(
            [item["image_url"]["url"] for item in request["images"]],
            ["https://example.com/first.jpg", "https://example.com/last.jpg"],
        )

        client = object.__new__(AdobeClient)
        payload = client._build_video_payload(
            video_conf={"engine": "seedance2", "resolution": "480p"},
            prompt=request["prompt"],
            aspect_ratio=request["ratio"],
            duration=request["duration"],
            source_image_ids=["first-id", "last-id"],
            negative_prompt=SEEDANCE_NEGATIVE_PROMPT,
            generate_audio=request["generate_audio"],
            seed=request["seed"],
        )
        self.assertEqual(
            payload,
            {
                "modelId": "seedance",
                "modelVersion": "seedance_2.0",
                "size": {"width": 854, "height": 480},
                "seeds": [202381],
                "referenceBlobs": [
                    {"id": "first-id", "usage": "frame", "order": 1},
                    {"id": "last-id", "usage": "frame", "order": 2},
                ],
                "prompt": "摘下苹果",
                "negativePrompt": SEEDANCE_NEGATIVE_PROMPT,
                "duration": 5,
                "generateAudio": False,
                "generationMetadata": {
                    "module": "text2video",
                    "submodule": "ff-video-generate",
                },
                "generationSettings": {"aspectRatio": "16:9"},
                "output": {"storeInputs": True},
            },
        )


if __name__ == "__main__":
    unittest.main()
