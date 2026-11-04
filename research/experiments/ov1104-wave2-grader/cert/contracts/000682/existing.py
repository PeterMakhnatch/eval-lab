import sys
sys.path.insert(0, '/testbed')

import unittest
from unittest.mock import MagicMock

from c7n.actions import BaseAction
from c7n.filters.kms import KmsRelatedFilter
from c7n.resources.sns import SNS
from c7n.utils import reset_session_cache
from tests.common import BaseTest


class TestSnsKmsAndEncryptionContract(BaseTest):

    def setUp(self):
        super(TestSnsKmsAndEncryptionContract, self).setUp()
        reset_session_cache()

    def tearDown(self):
        reset_session_cache()
        super(TestSnsKmsAndEncryptionContract, self).tearDown()

    def _make_action(self, data):
        reset_session_cache()
        sns_client = MagicMock()
        session = MagicMock()
        session.client.return_value = sns_client

        def session_factory():
            return session

        session_factory.region = 'us-east-1'
        manager = MagicMock()
        manager.session_factory = session_factory
        action_cls = SNS.action_registry.get('set-encryption')
        action = action_cls(data, manager)
        return action, session, sns_client

    def test_kms_filter_registration_and_expression(self):
        filter_cls = SNS.filter_registry.get('kms-key')
        self.assertIsNotNone(filter_cls)
        self.assertTrue(issubclass(filter_cls, KmsRelatedFilter))
        self.assertEqual(filter_cls.RelatedIdsExpression, 'KmsMasterKeyId')

    def test_set_encryption_registration_schema_and_permissions(self):
        action_cls = SNS.action_registry.get('set-encryption')
        self.assertIsNotNone(action_cls)
        self.assertTrue(issubclass(action_cls, BaseAction))
        self.assertIn('sns:SetTopicAttributes', action_cls.permissions)

        props = action_cls.schema.get('properties', {})
        self.assertEqual(props.get('key'), {'type': 'string'})
        self.assertEqual(props.get('enabled'), {'type': 'boolean'})

    def test_set_encryption_defaults_to_enabled_and_default_sns_alias(self):
        action, session, sns_client = self._make_action({})
        arn = 'arn:aws:sns:us-east-1:123456789012:topic-default'
        action.process([{'TopicArn': arn}])

        session.client.assert_called_with('sns')
        sns_client.set_topic_attributes.assert_called_once_with(
            TopicArn=arn,
            AttributeName='KmsMasterKeyId',
            AttributeValue='alias/aws/sns',
        )

    def test_set_encryption_explicit_enabled_true_uses_default_or_custom_key(self):
        arn = 'arn:aws:sns:us-east-1:123456789012:topic-enabled'

        action_default, _, sns_default = self._make_action({'enabled': True})
        action_default.process([{'TopicArn': arn}])
        sns_default.set_topic_attributes.assert_called_once_with(
            TopicArn=arn,
            AttributeName='KmsMasterKeyId',
            AttributeValue='alias/aws/sns',
        )

        custom_key = 'arn:aws:kms:us-east-1:123456789012:key/11112222-3333'
        action_custom, _, sns_custom = self._make_action(
            {'enabled': True, 'key': custom_key}
        )
        action_custom.process([{'TopicArn': arn}])
        sns_custom.set_topic_attributes.assert_called_once_with(
            TopicArn=arn,
            AttributeName='KmsMasterKeyId',
            AttributeValue=custom_key,
        )

    def test_set_encryption_custom_key_with_implicit_enabled(self):
        arn1 = 'arn:aws:sns:us-east-1:123456789012:topic-1'
        arn2 = 'arn:aws:sns:us-east-1:123456789012:topic-2'
        action, _, sns_client = self._make_action({'key': 'alias/custom/cmk'})
        action.process([{'TopicArn': arn1}, {'TopicArn': arn2}])

        self.assertEqual(sns_client.set_topic_attributes.call_count, 2)
        sns_client.set_topic_attributes.assert_any_call(
            TopicArn=arn1,
            AttributeName='KmsMasterKeyId',
            AttributeValue='alias/custom/cmk',
        )
        sns_client.set_topic_attributes.assert_any_call(
            TopicArn=arn2,
            AttributeName='KmsMasterKeyId',
            AttributeValue='alias/custom/cmk',
        )

    def test_set_encryption_disabled_clears_key_even_when_key_provided(self):
        arn = 'arn:aws:sns:us-east-1:123456789012:topic-disable'

        for cfg in ({'enabled': False}, {'enabled': False, 'key': 'alias/ignored'}):
            action, _, sns_client = self._make_action(cfg)
            action.process([{'TopicArn': arn}])
            sns_client.set_topic_attributes.assert_called_once_with(
                TopicArn=arn,
                AttributeName='KmsMasterKeyId',
                AttributeValue='',
            )

    def test_policy_validation_for_filter_and_action(self):
        policy = self.load_policy({
            'name': 'sns-kms-encryption-policy',
            'resource': 'sns',
            'filters': [
                {'type': 'kms-key', 'key': 'c7n:AliasName', 'value': 'alias/aws/sns'}
            ],
            'actions': [
                {'type': 'set-encryption', 'key': 'alias/custom', 'enabled': True}
            ],
        })
        self.assertIsNotNone(policy)


if __name__ == '__main__':
    unittest.main()
