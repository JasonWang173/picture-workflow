import base64
import hashlib
import hmac
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from food_compositor import oss_rest


class Response(io.BytesIO):
    pass


class OssRestTest(unittest.TestCase):
    def setUp(self):
        self.bucket = oss_rest.Bucket(oss_rest.Auth('test-id', 'test-secret'), 'https://oss-cn-beijing.aliyuncs.com', 'test-bucket')
        self.requests = []

    def open_request(self, request, timeout):
        self.requests.append(request)
        params = parse_qs(urlsplit(request.full_url).query)
        if request.method == 'GET' and 'list-type' in params:
            if 'continuation-token' not in params:
                return Response(b'<ListBucketResult><Contents><Key>prefix%2Fone.json</Key></Contents><IsTruncated>true</IsTruncated><NextContinuationToken>next</NextContinuationToken></ListBucketResult>')
            return Response(b'<ListBucketResult><Contents><Key>prefix%2Ftwo.json</Key></Contents><IsTruncated>false</IsTruncated></ListBucketResult>')
        return Response(b'content')

    def test_pagination_and_signature(self):
        with patch('urllib.request.urlopen', side_effect=self.open_request):
            objects = list(oss_rest.ObjectIteratorV2(self.bucket, prefix='prefix/'))
        self.assertEqual([obj.key for obj in objects], ['prefix/one.json', 'prefix/two.json'])
        self.assertEqual(len(self.requests), 2)
        request = self.requests[0]
        date = request.get_header('Date')
        message = f'GET\n\n\n{date}\n/test-bucket/'.encode()
        signature = base64.b64encode(hmac.new(b'test-secret', message, hashlib.sha1).digest()).decode()
        self.assertEqual(request.get_header('Authorization'), f'OSS test-id:{signature}')

    def test_put_and_delete(self):
        with patch('urllib.request.urlopen', side_effect=self.open_request):
            self.bucket.put_object('prefix/test.json', '{"ok":true}')
            self.bucket.delete_object('prefix/test.json')
        self.assertEqual([request.method for request in self.requests], ['PUT', 'DELETE'])
        self.assertEqual(self.requests[0].data, b'{"ok":true}')
        self.assertEqual(self.requests[0].get_header('Content-type'), 'application/json')

    def test_missing_key_and_invalid_endpoint(self):
        error = HTTPError('https://example.com', 404, 'not found', {}, None)
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(oss_rest.NoSuchKey):
                self.bucket.get_object('missing')
        with self.assertRaises(ValueError):
            oss_rest.Bucket(oss_rest.Auth('a', 'b'), 'http://example.com', 'test-bucket')


if __name__ == '__main__':
    unittest.main()
