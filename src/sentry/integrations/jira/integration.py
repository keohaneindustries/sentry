from __future__ import absolute_import

from django.core.urlresolvers import reverse
from django.utils.translation import ugettext_lazy as _

from sentry.integrations import (
    Integration, IntegrationFeatures, IntegrationProvider, IntegrationMetadata
)
from sentry.integrations.exceptions import ApiUnauthorized, IntegrationError
from sentry.integrations.issues import IssueSyncMixin

from .client import JiraApiClient


alert_link = {
    'text': 'Visit the **Atlassian Marketplace** to install this integration.',
    # TODO(jess): update this when we have our app listed on the
    # atlassian marketplace
    'link': 'https://marketplace.atlassian.com/',
}

metadata = IntegrationMetadata(
    description='Sync Sentry and JIRA issues.',
    author='The Sentry Team',
    noun=_('Instance'),
    issue_url='https://github.com/getsentry/sentry/issues/new?title=JIRA%20Integration:%20&labels=Component%3A%20Integrations',
    source_url='https://github.com/getsentry/sentry/tree/master/src/sentry/integrations/jira',
    aspects={
        'alert_link': alert_link,
    },
)

# A list of common builtin custom field types for JIRA for easy reference.
JIRA_CUSTOM_FIELD_TYPES = {
    'select': 'com.atlassian.jira.plugin.system.customfieldtypes:select',
    'textarea': 'com.atlassian.jira.plugin.system.customfieldtypes:textarea',
    'multiuserpicker': 'com.atlassian.jira.plugin.system.customfieldtypes:multiuserpicker',
    'tempo_account': 'com.tempoplugin.tempo-accounts:accounts.customfield'
}


class JiraIntegration(Integration, IssueSyncMixin):
    def get_link_issue_config(self, group, **kwargs):
        fields = super(JiraIntegration, self).get_link_issue_config(group, **kwargs)
        org = group.organization
        autocomplete_url = reverse(
            'sentry-extensions-jira-search', args=[org.slug, self.model.id],
        )
        for field in fields:
            if field['name'] == 'externalIssue':
                field['autocompleteUrl'] = autocomplete_url
        return fields

    def get_client(self):
        return JiraApiClient(
            self.model.metadata['base_url'],
            self.model.metadata['shared_secret'],
        )

    def get_issue(self, issue_id):
        client = self.get_client()
        issue = client.get_issue(issue_id)
        return {
            'title': issue['fields']['summary'],
            'description': issue['fields']['description'],
        }

    def create_comment(self, issue_id, comment):
        return self.get_client().create_comment(issue_id, comment)

    def search_issues(self, query):
        return self.get_client().search_issues(query)

    def make_choices(self, x):
        return [(y['id'], y['name'] if 'name' in y else y['value']) for y in x] if x else []

    def build_dynamic_field(self, group, field_meta):
        """
        Builds a field based on JIRA's meta field information
        """
        schema = field_meta['schema']

        # set up some defaults for form fields
        fieldtype = 'text'
        fkwargs = {
            'label': field_meta['name'],
            'required': field_meta['required'],
        }
        # override defaults based on field configuration
        if (schema['type'] in ['securitylevel', 'priority']
                or schema.get('custom') == JIRA_CUSTOM_FIELD_TYPES['select']):
            fieldtype = 'select'
            fkwargs['choices'] = self.make_choices(field_meta.get('allowedValues'))
        elif field_meta.get('autoCompleteUrl') and \
                (schema.get('items') == 'user' or schema['type'] == 'user'):
            pass
            # TODO(jess): implement autocomplete for users
            # fieldtype = 'select'
            # sentry_url = '/api/0/issues/%s/plugins/%s/autocomplete' % (group.id, self.slug)
            # fkwargs['url'] = '%s?jira_url=%s' % (
            #     sentry_url, quote_plus(field_meta['autoCompleteUrl']),
            # )
            # fkwargs['has_autocomplete'] = True
            # fkwargs['placeholder'] = 'Start typing to search for a user'
        elif schema['type'] in ['timetracking']:
            # TODO: Implement timetracking (currently unsupported alltogether)
            return None
        elif schema.get('items') in ['worklog', 'attachment']:
            # TODO: Implement worklogs and attachments someday
            return None
        elif schema['type'] == 'array' and schema['items'] != 'string':
            fieldtype = 'select'
            fkwargs.update(
                {
                    'multiple': True,
                    'choices': self.make_choices(field_meta.get('allowedValues')),
                    'default': []
                }
            )

        # break this out, since multiple field types could additionally
        # be configured to use a custom property instead of a default.
        if schema.get('custom'):
            if schema['custom'] == JIRA_CUSTOM_FIELD_TYPES['textarea']:
                fieldtype = 'textarea'

        fkwargs['type'] = fieldtype
        return fkwargs

    def get_issue_type_meta(self, issue_type, meta):
        issue_types = meta['issuetypes']
        issue_type_meta = None
        if issue_type:
            matching_type = [t for t in issue_types if t['id'] == issue_type]
            issue_type_meta = matching_type[0] if len(matching_type) > 0 else None

        # still no issue type? just use the first one.
        if not issue_type_meta:
            issue_type_meta = issue_types[0]

        return issue_type_meta

    # def get_new_issue_fields(self, request, group, event, **kwargs):
    def get_create_issue_config(self, group, **kwargs):
        fields = super(JiraIntegration, self).get_create_issue_config(group, **kwargs)
        params = kwargs.get('params', {})

        # TODO(jess): update if we allow saving a default project key

        client = self.get_client()
        try:
            resp = client.get_create_meta(params.get('projectKey'))
        except ApiUnauthorized:
            raise IntegrationError(
                'JIRA returned: Unauthorized. '
                'Please check your configuration settings.'
            )

        try:
            meta = resp['projects'][0]
        except IndexError:
            raise IntegrationError(
                'Error in JIRA configuration, no projects found.'
            )

        # check if the issuetype was passed as a parameter
        issue_type = params.get('issuetype')

        # TODO(jess): update if we allow specifying a default issuetype

        issue_type_meta = self.get_issue_type_meta(issue_type, meta)

        issue_type_choices = self.make_choices(meta['issuetypes'])

        # make sure default issue type is actually
        # one that is allowed for project
        if issue_type:
            if not any((c for c in issue_type_choices if c[0] == issue_type)):
                issue_type = issue_type_meta['id']

        fields = [
            {
                'name': 'project',
                'label': 'Jira Project',
                'choices': ((meta['key']), (meta['key'])),
                'default': meta['key'],
                'type': 'select',
            }
        ] + fields + [
            {
                'name': 'issuetype',
                'label': 'Issue Type',
                'default': issue_type or issue_type_meta['id'],
                'type': 'select',
                'choices': issue_type_choices
            }
        ]

        # title is renamed to summary before sending to JIRA
        standard_fields = [f['name'] for f in fields] + ['summary']

        # TODO(jess): are we going to allow ignored fields?
        # ignored_fields = (self.get_option('ignored_fields', group.project) or '').split(',')
        ignored_fields = set()

        # apply ordering to fields based on some known built-in JIRA fields.
        # otherwise weird ordering occurs.
        anti_gravity = {"priority": -150, "fixVersions": -125, "components": -100, "security": -50}

        dynamic_fields = issue_type_meta.get('fields').keys()
        dynamic_fields.sort(key=lambda f: anti_gravity.get(f) or 0)
        # build up some dynamic fields based on required shit.
        for field in dynamic_fields:
            if field in standard_fields or field in [x.strip() for x in ignored_fields]:
                # don't overwrite the fixed fields for the form.
                continue
            mb_field = self.build_dynamic_field(group, issue_type_meta['fields'][field])
            if mb_field:
                mb_field['name'] = field
                fields.append(mb_field)

        for field in fields:
            if field['name'] == 'priority':
                # whenever priorities are available, put the available ones in the list.
                # allowedValues for some reason doesn't pass enough info.
                field['choices'] = self.make_choices(client.get_priorities())
                # TODO(jess): fix if we are going to allow default priority
                # field['default'] = self.get_option('default_priority', group.project) or ''
                field['default'] = ''
            elif field['name'] == 'fixVersions':
                field['choices'] = self.make_choices(client.get_versions(meta['key']))

        return fields


class JiraIntegrationProvider(IntegrationProvider):
    key = 'jira'
    name = 'JIRA'
    metadata = metadata
    integration_cls = JiraIntegration

    features = frozenset([IntegrationFeatures.ISSUE_SYNC])

    can_add = False

    def get_pipeline_views(self):
        return []

    def build_integration(self, state):
        # Most information is not availabe during integration install time,
        # since the integration won't have been fully configired on JIRA's side
        # yet, we can't make API calls for more details like the server name or
        # Icon.
        return {
            'provider': 'jira',
            'external_id': state['clientKey'],
            'name': 'JIRA',
            'metadata': {
                'oauth_client_id': state['oauthClientId'],
                # public key is possibly deprecated, so we can maybe remove this
                'public_key': state['publicKey'],
                'shared_secret': state['sharedSecret'],
                'base_url': state['baseUrl'],
                'domain_name': state['baseUrl'].replace('https://', ''),
            },
        }
