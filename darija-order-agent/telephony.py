import os
from dotenv import load_dotenv

load_dotenv()
from db import get_order, create_call


def start_call(order_id: str) -> str:
    """Place the outbound Twilio call for a PENDING_CONFIRMATION order. Returns the call SID."""
    from twilio.rest import Client

    order = get_order(order_id)
    if not order:
        raise ValueError(f"Order not found: {order_id}")
    if order["status"] != "PENDING_CONFIRMATION":
        raise ValueError(f"Order status is {order['status']}, expected PENDING_CONFIRMATION")

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    call = client.calls.create(
        to=order["phone"],
        from_=os.environ["TWILIO_FROM_NUMBER"],
        url=f"{base}/voice?order_id={order_id}",
        method="POST",
        status_callback=f"{base}/call-status?order_id={order_id}",
        status_callback_method="POST",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        timeout=25,
    )
    create_call(call.sid, order_id)
    return call.sid
