"""The email layout tool keeps an alert's shape and its authentication, and masks the data."""

import email.message
import email.policy
from email.parser import BytesParser

from tools.email_layout import describe


def test_values_are_masked_and_the_shape_and_dkim_stay() -> None:
    message = email.message.EmailMessage()
    message["From"] = "GX Bank <alerts@gxbank.my>"
    message["To"] = "someone.private@gmail.com"
    message["Subject"] = "You paid RM12.50 to Kedai Ali"
    message["Authentication-Results"] = (
        "mx.google.com; dkim=pass header.i=@gxbank.my header.s=s1; spf=pass; dmarc=pass"
    )
    message["DKIM-Signature"] = "v=1; a=rsa-sha256; d=gxbank.my; s=s1; b=abc"
    message["X-Forwarded-To"] = "someone.private@gmail.com"
    message["Return-Path"] = "<someone.private+caf_=fwd=gmail.com@gmail.com>"
    message["Received-SPF"] = (
        "pass (domain of someone.private+caf_=fwd@gmail.com) smtp.mailfrom=a.b@x.my"
    )
    message.set_content("Dear Tan Mei Ling,\nYou have paid RM12.50 to Kedai Ali on 17 Nov 2026.")
    message.add_alternative(
        "<html><body><table><tr><td>Amount</td><td>RM12.50</td></tr>"
        "<tr><td>Merchant</td><td>Kedai Ali</td></tr></table>"
        "<script>track()</script></body></html>",
        subtype="html",
    )
    parsed = BytesParser(policy=email.policy.default).parsebytes(message.as_bytes())
    layout = "\n".join(describe(parsed))  # pyright: ignore[reportArgumentType]

    for private in ("someone.private", "Tan", "Mei", "Ling", "Kedai", "Ali", "12.50", "17"):
        assert private not in layout, private
    assert "From: GX Bank <alerts@gxbank.my>" in layout
    assert "Subject: You paid RM99.99 to Xxxxx Xxx" in layout
    assert "dkim=pass header.i=@gxbank.my" in layout
    assert "Return-Path: <xxx@gmail.com>" in layout
    assert "domain of xxx@gmail.com) smtp.mailfrom=xxx@x.my" in layout
    assert "DKIM-Signature: d=gxbank.my s=s1" in layout
    assert "forwarded by Gmail filter: yes" in layout
    assert "Dear Xxx Xxx Xxxx," in layout
    assert "Amount | RM99.99" in layout
    assert "Merchant | Xxxxx Xxx" in layout
    assert "track" not in layout
