import logging
import re


class RedactAuthLinks(logging.Filter):
    def filter(self, record):
        # Do not persist password reset or email confirmation tokens in logs.
        record.msg = re.sub(r'/(?:password/reset|account/email/confirm)/[^\s\"\'<>]*',
                            '/auth/[redacted]', record.getMessage())
        record.args = ()
        return True
