"""
Signals the lending app listens to. The payments app never imports lending
code; lending reacts to these instead. Each sends `intent=PaymentIntent`.

    from payments.signals import payment_succeeded

    @receiver(payment_succeeded)
    def settle_repayment(sender, intent, **kwargs):
        if intent.source_ref.startswith("repayment:"):
            ...mark the schedule item paid...
"""
from django.dispatch import Signal

payment_succeeded = Signal()        # intent
payment_failed = Signal()           # intent
payment_action_required = Signal()  # intent (intent.required_action says what)
payment_needs_attention = Signal()  # intent, exception (ReconciliationException)
