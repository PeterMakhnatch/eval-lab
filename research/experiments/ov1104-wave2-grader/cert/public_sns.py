"""Minimal public SNS side-effect checks; no exact schema/return-value requirements."""
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, '/testbed')

from c7n.resources import sns
from c7n.filters.kms import KmsRelatedFilter


class SNSClient:
    def __init__(self):
        self.calls = []

    def set_topic_attributes(self, **kwargs):
        self.calls.append(kwargs)


class PublicSNS(unittest.TestCase):
    def exercise(self, data, expected_key):
        client = SNSClient()
        session = types.SimpleNamespace(client=lambda service: client if service == 'sns' else None)
        manager = types.SimpleNamespace(session_factory=object())
        resources = [{'TopicArn': 'arn:aws:sns:us-east-1:123456789012:first'},
                     {'TopicArn': 'arn:aws:sns:us-east-1:123456789012:second'}]
        action = sns.SNS.action_registry['set-encryption'](data, manager)
        with patch.object(sns, 'local_session', return_value=session):
            action.process(resources)
        self.assertEqual(client.calls, [dict(TopicArn=r['TopicArn'], AttributeName='KmsMasterKeyId',
            AttributeValue=expected_key) for r in resources])

    def test_selected_key_configures_every_topic(self):
        self.exercise({'type': 'set-encryption', 'state': True, 'key': 'alias/approved'}, 'alias/approved')

    def test_disable_clears_encryption(self):
        self.exercise({'type': 'set-encryption', 'state': False, 'key': 'alias/ignored'}, '')

    def test_candidates_reuse_shared_kms_filter(self):
        cls = sns.SNS.filter_registry['kms-key']
        self.assertTrue(issubclass(cls, KmsRelatedFilter))
        self.assertEqual(cls.RelatedIdsExpression, 'KmsMasterKeyId')


if __name__ == '__main__':
    unittest.main()
