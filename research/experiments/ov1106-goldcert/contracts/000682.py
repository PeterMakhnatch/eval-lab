"""Ground: SNS policy interface, selected-key encryption, disable, and KMS attributes.

The instruction specifies no registration names or config keys. Discover these
through the public registries and JSON schemas, not implementation source.
"""
import itertools
import sys
import types
import unittest
import uuid

sys.path.insert(0, '/testbed')
from c7n.resources.sns import SNS
from c7n.config import Config
from c7n.policy import Policy


KEY_ID = '11111111-2222-3333-4444-555555555555'
KEY_ARN = 'arn:aws:kms:us-east-1:123456789012:key/' + KEY_ID
ALIAS = 'alias/approved'
TOPICS = [{'TopicArn': 'arn:aws:sns:us-east-1:123456789012:first', 'KmsMasterKeyId': KEY_ARN},
          {'TopicArn': 'arn:aws:sns:us-east-1:123456789012:second', 'KmsMasterKeyId': ''}]


class Service:
    def __init__(self):
        self.writes = []

    def set_topic_attributes(self, **kwargs):
        self.writes.append(kwargs)
        return {}

    def describe_key(self, **kwargs):
        return {'KeyMetadata': {'KeyId': KEY_ID, 'Arn': KEY_ARN, 'KeyState': 'Enabled'}}

    def list_keys(self, **kwargs):
        return {'Keys': [{'KeyId': KEY_ID, 'KeyArn': KEY_ARN}], 'Truncated': False}

    def list_aliases(self, **kwargs):
        return {'Aliases': [{'AliasName': ALIAS, 'TargetKeyId': KEY_ID}], 'Truncated': False}

    def can_paginate(self, name):
        return False

    def get_paginator(self, name):
        return types.SimpleNamespace(paginate=lambda **kwargs: [{'ResourceTagMappingList': []}])


class SessionFactory:
    def __init__(self, service):
        self.region = 'public-' + uuid.uuid4().hex
        self.service = service

    def __call__(self, *args, **kwargs):
        return types.SimpleNamespace(client=lambda *a, **k: self.service)


def action_configs():
    for name, cls in SNS.action_registry.items():
        properties = getattr(cls, 'schema', {}).get('properties', {})
        booleans = [k for k, v in properties.items() if k != 'type' and v.get('type') == 'boolean']
        strings = [k for k, v in properties.items() if k != 'type' and v.get('type') == 'string']
        for boolean, string in itertools.product(booleans, strings):
            yield name, cls, boolean, string


class PublicSNS(unittest.TestCase):
    def test_encryption_policy_exposes_selected_key_and_disable(self):
        successes, failures = [], []
        for name, cls, boolean, string in action_configs():
            service = Service()
            manager = types.SimpleNamespace(session_factory=SessionFactory(service),
                config=Config.empty(region='us-east-1', account_id='123456789012'))
            try:
                for enabled, selected in [(True, ALIAS), (True, KEY_ID), (True, KEY_ARN), (False, ALIAS)]:
                    service.writes[:] = []
                    cls({'type': name, boolean: enabled, string: selected}, manager).process([dict(r) for r in TOPICS])
                    self.assertEqual(len(service.writes), 2)
                    self.assertEqual({r['TopicArn'] for r in service.writes}, {r['TopicArn'] for r in TOPICS})
                    for call in service.writes:
                        self.assertEqual(call['AttributeName'], 'KmsMasterKeyId')
                        # AWS accepts the selected key directly or its resolved ID/ARN.
                        self.assertIn(call['AttributeValue'], {selected, KEY_ID, KEY_ARN} if enabled else {''})
                successes.append(name)
            except Exception as error:
                failures.append((name, repr(error)))
        self.assertTrue(successes, 'No public policy action meets encryption/disable behavior: %r' % failures)

    def test_kms_attribute_and_alias_filtering(self):
        successes, failures = [], []
        # Baseline public value-filter conventions supply key/value/op. The new registration name is unconstrained.
        for name, cls in SNS.filter_registry.items():
            service = Service()
            factory = SessionFactory(service)
            try:
                for key, value in [('KeyId', KEY_ID), ('Arn', KEY_ARN), ('c7n:AliasName', ALIAS), ('KeyState', 'Enabled')]:
                    policy = Policy({'name': 'public-kms', 'resource': 'sns',
                        'filters': [{'type': name, 'key': key, 'value': value}]},
                        Config.empty(region='us-east-1', account_id='123456789012'), session_factory=factory)
                    filters = policy.resource_manager.filters
                    result = filters[0].process([dict(r) for r in TOPICS])
                    self.assertEqual([r['TopicArn'] for r in result], [TOPICS[0]['TopicArn']])
                # Inverting an approved key match must select the other topic.
                policy = Policy({'name': 'public-not-kms', 'resource': 'sns', 'filters': [
                    {'not': [{'type': name, 'key': 'c7n:AliasName', 'value': ALIAS}]}]},
                    Config.empty(region='us-east-1', account_id='123456789012'), session_factory=factory)
                result = policy.resource_manager.filters[0].process([dict(r) for r in TOPICS])
                self.assertEqual([r['TopicArn'] for r in result], [TOPICS[1]['TopicArn']])
                successes.append(name)
            except Exception as error:
                failures.append((name, repr(error)))
        self.assertTrue(successes, 'No public policy filter meets KMS matching behavior: %r' % failures)


if __name__ == '__main__':
    unittest.main()
