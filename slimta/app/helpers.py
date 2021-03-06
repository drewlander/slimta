# Copyright (c) 2013 Ian C. Good
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#


import math
from functools import wraps
from socket import getfqdn, gethostname

from slimta.edge.smtp import SmtpValidators
from slimta.edge.wsgi import WsgiValidators, WsgiResponse
from slimta.smtp.auth import Auth, CredentialsInvalidError
from slimta.util import build_auth_from_dict
from slimta.util.dnsbl import check_dnsbl, DnsBlocklistGroup

from slimta.policy.forward import Forward
from slimta.policy.split import RecipientSplit, RecipientDomainSplit
from slimta.policy.spamassassin import SpamAssassin
from slimta.policy.headers import AddDateHeader, AddMessageIdHeader, \
                                  AddReceivedHeader


def _get_spamassassin_object(options):
    host = options.get('host', 'localhost')
    port = int(options.get('port', 783))
    return SpamAssassin((host, port))


class RuleHelpers(object):

    @staticmethod
    def _noop_decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            return f(*args, **kwargs)
        return wrapper

    def __init__(self, options):
        rules = options.get('rules', {})
        self.banner = fill_hostname_template(rules.get('banner'))
        self.dnsbl = rules.get('dnsbl')
        self.only_senders = rules.get('only_senders')
        self.only_rcpts = rules.get('only_recipients')
        self.credentials = rules.get('require_credentials')
        self.reject_spf = rules.get('reject_spf')
        self.scanner = self._get_scanner(rules.get('reject_spam'))

    def _get_scanner(self, options):
        if options is None:
            return None
        type = options.get('type', 'spamassassin')
        if type == 'spamassassin':
            return _get_spamassassin_object(options)
        return None

    def is_sender_ok(self, validators, sender):
        if self.only_senders is not None and sender not in self.only_senders:
            return False
        if self.credentials is not None and not validators.session.auth_result:
            return False
        return True

    def is_recipient_ok(self, recipient):
        if self.only_rcpts is not None and recipient not in self.only_rcpts:
            return False
        return True

    def get_banner_decorator(self):
        if self.dnsbl:
            if isinstance(self.dnsbl, list):
                blgroup = DnsBlocklistGroup()
                for bl in self.dnsbl:
                    blgroup.add_dnsbl(bl)
                return check_dnsbl(blgroup, match_code='520')
            else:
                return check_dnsbl(self.dnsbl, match_code='520')
        return self._noop_decorator

    def get_mail_decorator(self):
        if self.reject_spf:
            from slimta.spf import EnforceSpf
            spf = EnforceSpf()
            msg = '5.7.1 Access denied; {reason}'
            for spf_type in self.reject_spf:
                spf.set_enforcement(spf_type, match_code='550',
                                    match_message=msg)
            return spf.check
        return self._noop_decorator

    def set_banner_message(self, reply):
        if self.banner:
            reply.message = self.banner

    def reject_spam(self, data):
        if self.scanner:
            is_spam, info = self.scanner.scan(data)
            return is_spam
        return False


def build_smtpedge_validators(options):
    rules = RuleHelpers(options)
    class CustomValidators(SmtpValidators):
        @rules.get_banner_decorator()
        def handle_banner(self, reply, address):
            rules.set_banner_message(reply)
        @rules.get_mail_decorator()
        def handle_mail(self, reply, sender):
            if not rules.is_sender_ok(self, sender):
                reply.code = '550'
                reply.message = '5.7.1 Sender <{0}> Not allowed'.format(sender)
        def handle_rcpt(self, reply, rcpt):
            if not rules.is_recipient_ok(rcpt):
                reply.code = '550'
                reply.message = '5.7.1 Recipient <{0}> Not allowed'.format(rcpt)
        def handle_have_data(self, reply, data):
            if rules.reject_spam(data):
                reply.code = '554'
                reply.message = '5.6.0 Message content rejected'
    return CustomValidators


def build_smtpedge_auth(options):
    rules = RuleHelpers(options)
    if rules.credentials is None:
        return None
    return build_auth_from_dict(rules.credentials)


def build_wsgiedge_validators(options):
    rules = RuleHelpers(options)
    class CustomValidators(WsgiValidators):
        def validate_sender(self, sender):
            if not rules.is_sender_ok(self, sender):
                smtp_code = '550'
                smtp_message = '5.7.1 Sender <{0}> Not allowed'.format(sender)
                reply = '{0}; message="{1}"'.format(smtp_code, smtp_message)
                raise WsgiResponse('403 Forbidden', [('X-Smtp-Reply', reply)])
        def validate_recipient(self, rcpt):
            if not rules.is_recipient_ok(rcpt):
                smtp_code = '550'
                smtp_message = '5.7.1 Recipient <{0}> Not allowed'.format(rcpt)
                reply = '{0}; message="{1}"'.format(smtp_code, smtp_message)
                raise WsgiResponse('403 Forbidden', [('X-Smtp-Reply', reply)])
    return CustomValidators


def add_queue_policies(queue, policy_options):
    for policy in policy_options:
        if policy.type == 'add_date_header':
            queue.add_policy(AddDateHeader())
        elif policy.type == 'add_messageid_header':
            hostname = policy.get('hostname')
            queue.add_policy(AddMessageIdHeader(hostname))
        elif policy.type == 'add_received_header':
            queue.add_policy(AddReceivedHeader())
        elif policy.type == 'recipient_split':
            queue.add_policy(RecipientSplit())
        elif policy.type == 'recipient_domain_split':
            queue.add_policy(RecipientDomainSplit())
        elif policy.type == 'forward':
            forward = Forward()
            for pattern, repl in dict(policy.get('mapping', {})).items():
                forward.add_mapping(pattern, repl)
            queue.add_policy(forward)
        elif policy.type == 'spamassassin':
            queue.add_policy(_get_spamassassin_object(policy))


def fill_hostname_template(val):
    if not val:
        return val
    return val.format(fqdn=getfqdn(),
                      hostname=gethostname())


def build_backoff_function(retry):
    if not retry:
        def no_retries(envelope, attempts):
            return None
        return no_retries
    maximum = int(retry.get('maximum', 0))
    delay = retry.get('delay', '300')
    delay_func = eval('lambda x: '+delay, math.__dict__)
    def backoff(envelope, attempts):
        if attempts > maximum:
            return None
        return delay_func(attempts)
    return backoff


def get_relay_credentials(creds):
    return (creds.username, creds.password)


# vim:et:fdm=marker:sts=4:sw=4:ts=4
