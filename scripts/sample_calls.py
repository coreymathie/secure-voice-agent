# Corey Mathie, 2026
# ruff: noqa: E501  (conversation templates read better unwrapped)
"""
Recent member calls for the fictional Cypress Harbor Credit Union: the records behind the
console's call list and call detail pages.

Every call is generated here from a seed: the members, phone numbers, amounts and transcripts
are invented. Phone numbers use the 555-01xx range reserved for fiction, emails use example.com,
and card numbers appear only as "ending in" digits. The intent mix, containment by intent and the
safeguards each call goes through follow the same assumptions as generate_sample_company.py, and
the controls named on each call are the ones this repo implements (policy gate, step-up
verification, SIM-swap signal, velocity and per-call caps, keypad payments, PII scrubbing, the
hash-chained audit log).

build_calls(rng, day, count) returns the most recent `count` calls on `day`, newest first.
"""

from __future__ import annotations

import random
from datetime import date, datetime

AGENT = "Harbor"  # the voice agent's persona name

FIRST = (
    "Maria Jose Luis Ana Carlos Sofia Daniel Gabriela Miguel Valentina Jean Marie Pierre Nadege Wilson Fabienne "
    "James Linda Robert Patricia Michael Barbara David Susan William Karen Richard Nancy Thomas Lisa Marcus "
    "Keisha Andre Tamika Darnell Latoya Jamal Aaliyah Rachel Aaron Miriam Jacob Leah Samuel Deborah Priya "
    "Rahul Mei Kevin Thanh Hoang Elena Dmitri Olga Giovanni Francesca Brian Megan Tyler Ashley Chris Jessica "
    "Ramon Yolanda Hector Beatriz Alejandro Camila Rosa Ernesto Ines Marisol Dwayne Shanice Terrence Monique"
).split()
LAST = (
    "Rodriguez Gonzalez Hernandez Lopez Martinez Perez Sanchez Ramirez Torres Flores Rivera Gomez Diaz Cruz "
    "Morales Reyes Jean-Baptiste Pierre Joseph Charles Louis Etienne Desir Baptiste Smith Johnson Williams "
    "Brown Jones Miller Davis Wilson Anderson Taylor Thomas Moore Jackson White Harris Thompson Robinson "
    "Walker Young Allen King Wright Scott Green Baker Adams Nelson Hill Campbell Mitchell Roberts Carter "
    "Phillips Evans Turner Cohen Goldberg Levy Shapiro Friedman Patel Shah Nguyen Tran Chen Wang Kim Park "
    "Ivanova Petrov Rossi Russo Bianchi Murphy O'Brien Sullivan Kelly Castillo Vargas Mendoza Ortiz Ruiz"
).split()
AREA = ["954", "954", "954", "561", "561", "305", "754"]
BRANCHES = [
    "Fort Lauderdale (Las Olas)",
    "Coral Springs",
    "Boca Raton",
    "Pompano Beach",
    "Plantation",
    "Hollywood",
    "Delray Beach",
    "Sunrise",
    "Weston",
    "Deerfield Beach",
    "Miramar",
]
HOURS = {
    "Fort Lauderdale (Las Olas)": "9 to 5 weekdays and 9 to 1 on Saturday",
    "Weston": "9 to 6 weekdays and 9 to 1 on Saturday",
}
LOANS = [
    ("auto loan", 287.43, 612.90),
    ("personal loan", 142.18, 389.55),
    ("home equity line", 318.07, 905.12),
    ("credit card", 35.00, 1284.62),
    ("boat loan", 266.39, 498.80),
]
MERCHANTS = [
    "a gas station in Davie",
    "an online electronics store",
    "a hotel in Orlando",
    "a streaming service",
    "a pharmacy in Plantation",
    "a restaurant on Las Olas",
    "a parking garage in Miami",
    "a rideshare app",
]

# (intent, share of calls, containment once tuned): matches generate_sample_company.INTENTS
INTENT_MIX = [
    ("Balance and recent transactions", 0.21, 0.91),
    ("Loan payment", 0.16, 0.78),
    ("Lost or stolen card", 0.11, 0.62),
    ("Dispute a card charge", 0.09, 0.55),
    ("Branch hours and locations", 0.08, 0.97),
    ("Appointment with a loan officer", 0.07, 0.83),
    ("Auto or home loan rates", 0.07, 0.69),
    ("Update address, phone or email", 0.06, 0.41),
    ("Fraud alert confirmation", 0.05, 0.58),
    ("Online banking login help", 0.06, 0.52),
    ("Other", 0.04, 0.22),
]
EXTRA_TRANSFER = {
    "Balance and recent transactions": 0.05,
    "Loan payment": 0.16,
    "Appointment with a loan officer": 0.15,
}
SPANISH_OK = {"Balance and recent transactions", "Loan payment", "Lost or stolen card", "Branch hours and locations"}

TRANSFER_REASONS = {
    "asked": "Caller asked for a person",
    "stepup": "Step-up verification not completed",
    "risk": "Risk score above threshold",
    "unsupported": "Intent not supported yet",
    "cap": "Payment over the per-call limit",
    "complaint": "Complaint or hardship request",
}


class Call:
    """Builds one call's transcript, safeguards and outcome as the conversation is written."""

    def __init__(self, rng: random.Random, member: dict, lang: str) -> None:
        self.rng = rng
        self.m = member
        self.lang = lang
        self.t = 0
        self.turns: list[dict] = []
        self.safeguards: list[dict] = []
        self.actions: list[dict] = []
        self.flags: list[str] = []
        self.verified = "not needed"
        self.risk = 0
        self.signals: list[str] = []

    def say(self, who: str, text: str, gap: tuple[int, int] = (3, 10)) -> None:
        self.t += self.rng.randint(*gap)
        self.turns.append({"t": self.t, "who": who, "text": text})
        self.t += max(2, len(text) // 8)  # speaking time, about 2 words a second

    def agent(self, text: str) -> None:
        self.say("agent", text)

    def member(self, text: str) -> None:
        self.say("member", text, (2, 6))

    def system(self, text: str) -> None:
        self.turns.append({"t": self.t, "who": "system", "text": text})
        self.t += 1

    def guard(self, control: str, result: str, kind: str = "pass") -> None:
        self.safeguards.append({"t": self.t, "control": control, "result": result, "kind": kind})

    def verify(self, ok: bool = True, swapped: bool = False) -> bool:
        es = self.lang == "es"
        last2 = self.m["phone"][-2:]
        if swapped:
            self.guard("SIM-swap signal", "carrier reported a SIM change on the phone on file 2 days ago", "block")
            self.agent(
                "Para proteger su cuenta, no puedo enviar un código a ese número ahora mismo."
                if es
                else "For your protection I can't send a code to the phone on file right now. A specialist can "
                "verify you another way."
            )
            self.verified = "held: SIM swap reported"
            self.flags.append("SIM-swap hold")
            return False
        self.guard("Step-up verification", f"one-time code sent by SMS to the phone on file ending in {last2}")
        self.agent(
            f"Le envié un código de seis dígitos al teléfono que termina en {last2}. ¿Me lo puede leer?"
            if es
            else f"I've just texted a six-digit code to the mobile number on file ending in {last2}. "
            "Could you read it back to me?"
        )
        code = f"{self.rng.randint(0, 999999):06d}"
        spoken = " ".join(code[:3]) + ", " + " ".join(code[3:])
        if ok:
            self.member(spoken)
            self.guard("Step-up verification", "code verified")
            self.agent("Gracias, ya está verificado." if es else "Thank you, you're verified.")
            self.verified = "verified by SMS code"
            return True
        self.member(spoken)
        self.agent(
            "Ese código no coincide. ¿Lo puede revisar?"
            if es
            else "That code didn't match. Could you check the message and read it again?"
        )
        wrong = f"{self.rng.randint(0, 999999):06d}"
        self.member(" ".join(wrong[:3]) + ", " + " ".join(wrong[3:]))
        self.agent("Tampoco coincide." if es else "That one didn't match either.")
        self.member(" ".join(f"{self.rng.randint(0, 999999):06d}"))
        self.guard("Step-up verification", "3 wrong codes: locked for this call, audited", "block")
        self.verified = "failed: 3 wrong codes"
        self.flags.append("Verification lockout")
        return False

    def opening(self, state_consent: bool) -> None:
        es = self.lang == "es"
        self.system("AI disclosure played before the agent joined")
        self.guard("AI disclosure", "played by the phone system before the agent connected")
        if state_consent:
            self.system("Recording consent: member pressed 1")
            self.guard("Recording consent", "asked (all-party consent state) and granted")
        self.guard("Caller lookup", "caller ID matched a member profile (used for lookup only, not identity)")
        if es:
            self.agent(
                f"Gracias por llamar a Cypress Harbor Credit Union. Soy {AGENT}, el asistente virtual. "
                "¿En qué le puedo ayudar?"
            )
        else:
            self.agent(
                f"Thanks for calling Cypress Harbor Credit Union, this is {AGENT}, your virtual assistant. "
                "How can I help you today?"
            )

    def transfer(self, reason_key: str, line: str | None = None) -> str:
        es = self.lang == "es"
        self.agent(
            line
            or (
                "Le voy a comunicar con un especialista. Ya tiene el resumen de su llamada."
                if es
                else "I'm connecting you with a member services specialist now. They'll have a summary of our "
                "conversation, so you won't need to repeat yourself."
            )
        )
        self.system("Transferred to member services with the call summary attached")
        self.guard("Handoff", TRANSFER_REASONS[reason_key], "handoff")
        return reason_key


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _balance(c: Call, rng: random.Random) -> tuple[str, str | None]:
    es = c.lang == "es"
    chk = rng.uniform(180, 6400)
    sav = rng.uniform(40, 18500)
    if es:
        c.member(
            rng.choice(["Quiero saber cuánto tengo en mi cuenta.", "¿Me puede dar el saldo de mi cuenta de cheques?"])
        )
        c.agent("Con gusto. Primero necesito verificar su identidad.")
    else:
        c.member(
            rng.choice(
                [
                    "Hi, I just want to check my balance.",
                    "Can you tell me how much is in my checking?",
                    "I need to know if my paycheck hit yet.",
                    "What's my available balance? And did a check for my landlord clear?",
                    "Yeah, balance on savings and checking please.",
                ]
            )
        )
        c.agent("Sure. Before I read any balances I need to quickly verify it's you.")
    ok = rng.random() > 0.04
    if not c.verify(ok):
        return "stepup", None
    c.guard("Policy gate", "read-only account inquiry allowed after verification")
    if es:
        c.agent(f"Su cuenta de cheques tiene {_money(chk)} disponibles y su cuenta de ahorros {_money(sav)}.")
        c.member("Perfecto, gracias.")
    else:
        dep = rng.choice(
            [
                "a direct deposit from Broward Health",
                "a payroll deposit from the School Board",
                "a Social Security deposit",
                "a mobile check deposit",
            ]
        )
        c.agent(
            f"Your Everyday Checking has {_money(chk)} available, and your Share Savings has {_money(sav)}. "
            f"The most recent transaction is {dep} of {_money(rng.uniform(780, 3400))} this morning."
        )
        c.member(rng.choice(["Perfect, that's all I needed.", "Great, thank you.", "Okay good. Thanks."]))
    c.agent("Is there anything else I can help with?" if not es else "¿Algo más en que le pueda ayudar?")
    c.member("No, that's it." if not es else "No, eso es todo.")
    return "resolved", None


def _payment(c: Call, rng: random.Random) -> tuple[str, str | None]:
    es = c.lang == "es"
    loan, lo, hi = rng.choice(LOANS)
    amount = round(rng.uniform(lo, hi), 2)
    if loan == "credit card" and rng.random() < 0.3:
        amount = 35.00
    if es:
        c.member(f"Quiero hacer el pago de mi préstamo de auto, son {_money(amount)}.")
        loan = "auto loan"
    else:
        c.member(
            rng.choice(
                [
                    f"I'd like to make my {loan} payment.",
                    f"Hi, I need to pay my {loan}, it's due today.",
                    f"Can I make a payment on my {loan} over the phone?",
                    f"I want to pay {_money(amount)} toward my {loan}.",
                ]
            )
        )
        c.agent(f"I can help with that. How much would you like to pay on your {loan}?")
        c.member(
            rng.choice([f"{_money(amount)}.", f"The regular amount, {_money(amount)}.", f"Let's do {_money(amount)}."])
        )
    over_cap = amount > 5000
    if not c.verify(rng.random() > 0.05):
        return "stepup", None
    if over_cap:
        c.guard("Per-call cap", f"{_money(amount)} is over the $5,000 per-call limit", "handoff")
        return c.transfer("cap"), None
    keypad = not es and (loan == "credit card" or rng.random() < 0.35)
    c.guard("Policy gate", f"take_payment allowed: {_money(amount)}, high tier, verified caller")
    c.guard("Velocity limit", "1 of 1 payment links in 2 minutes; 1 of 3 today")
    if keypad:
        c.agent(
            "I'll move you to our secure keypad line. Please enter your card number on your phone's keypad. "
            "I can't hear the digits, and the recording pauses while you type."
        )
        c.system("Keypad payment: recording paused, transcript suppressed, card entered on the keypad")
        c.guard("Keypad payment (PCI)", "card captured by the payment processor; never heard by the agent or recorded")
        c.system("Payment processor result: success")
        c.agent(
            f"You're back with me. Your payment of {_money(amount)} went through. A receipt is on its way by email."
        )
        c.actions.append(
            {
                "tool": "take_payment",
                "status": "keypad_paid",
                "amount_usd": amount,
                "detail": f"{loan.capitalize()} payment by keypad",
            }
        )
    else:
        c.agent(
            f"I've texted a secure payment link for {_money(amount)} to the mobile number on file. "
            "It's good for 30 minutes."
            if not es
            else f"Le envié un enlace seguro de pago por {_money(amount)} a su celular."
        )
        c.guard("Payment destination pinned", "link sent only to the phone on file")
        c.actions.append(
            {
                "tool": "take_payment",
                "status": "link_sent",
                "amount_usd": amount,
                "detail": f"{loan.capitalize()} payment link",
            }
        )
        c.member("Got it, I see the text." if not es else "Sí, ya me llegó.")
    c.flags.append("Payment")
    return "resolved", None


def _lost_card(c: Call, rng: random.Random) -> tuple[str, str | None]:
    es = c.lang == "es"
    last4 = f"{rng.randint(0, 9999):04d}"
    if es:
        c.member("Perdí mi tarjeta de débito, creo que se me cayó en el supermercado.")
    else:
        c.member(
            rng.choice(
                [
                    "I think I lost my debit card. I can't find it anywhere.",
                    "My wallet was stolen out of my car last night.",
                    "I left my credit card at a restaurant and they say they don't have it.",
                    "I need to cancel my card, it's gone.",
                ]
            )
        )
    c.agent(
        "I'm sorry about that. Let's lock it right away. First I need to verify you."
        if not es
        else "Lo siento. Vamos a bloquearla ahora mismo. Primero necesito verificarle."
    )
    if not c.verify(rng.random() > 0.06):
        return "stepup", None
    c.agent(
        f"I see a debit card ending in {last4}. Is that the one?"
        if not es
        else f"Veo una tarjeta de débito que termina en {last4}. ¿Es esa?"
    )
    c.member("Yes, that's it." if not es else "Sí, esa.")
    ticket = f"CS-{rng.randint(104000, 109999)}"
    c.guard("Policy gate", "card lock and replacement allowed for a verified caller")
    c.actions.append(
        {
            "tool": "create_ticket",
            "status": "created",
            "ticket": ticket,
            "detail": f"Card ending {last4} locked, replacement ordered",
        }
    )
    recent = rng.random() < 0.3
    c.agent(
        f"Done. The card ending in {last4} is locked and a replacement will arrive in 5 to 7 business days. "
        f"Your case number is {ticket}."
        if not es
        else f"Listo. La tarjeta está bloqueada y le enviaremos una nueva en 5 a 7 días. Su caso es {ticket}."
    )
    if recent and not es:
        c.member(f"Wait, is there anything on it I didn't make? There might be a charge at {rng.choice(MERCHANTS)}.")
        c.agent(
            "I see two pending charges since yesterday. I'll connect you to our card team so they can review them with you."
        )
        return c.transfer(
            "unsupported",
            "Our card services team will review those charges with you now. I'm transferring you with the case attached.",
        ), None
    c.member("Thank you so much." if not es else "Muchas gracias.")
    return "resolved", None


def _dispute(c: Call, rng: random.Random) -> tuple[str, str | None]:
    merchant = rng.choice(MERCHANTS)
    amt = round(rng.uniform(18, 940), 2)
    last4 = f"{rng.randint(0, 9999):04d}"
    c.member(
        rng.choice(
            [
                f"There's a charge on my card from {merchant} for {_money(amt)} that I didn't make.",
                f"I was charged twice at {merchant}, {_money(amt)} each time.",
                f"I don't recognize a {_money(amt)} charge from {merchant}.",
            ]
        )
    )
    c.agent("I can open a dispute for that. I'll need to verify you first.")
    if not c.verify(rng.random() > 0.05):
        return "stepup", None
    if rng.random() < 0.25:
        c.member(f"My card number is ending {last4}, the full number is 4{rng.randint(100, 999)} ...")
        c.agent("You don't need to read me the card number. I can see the card on your account.")
        c.guard("PII scrub", "card number removed from the transcript and the dispute case", "scrub")
        c.flags.append("Card number scrubbed")
    ticket = f"CS-{rng.randint(104000, 109999)}"
    c.guard("Policy gate", "dispute case allowed for a verified caller")
    c.actions.append(
        {
            "tool": "create_ticket",
            "status": "created",
            "ticket": ticket,
            "detail": f"Dispute: {_money(amt)} at {merchant}",
        }
    )
    c.agent(
        f"I've opened dispute case {ticket} for {_money(amt)}. A provisional credit decision is made within 10 business days, and you'll get updates by email."
    )
    if rng.random() < 0.35:
        c.member("Can I talk to someone about it now? This is the second time this happened.")
        return c.transfer("asked"), None
    c.member("Okay, thank you.")
    return "resolved", None


def _hours(c: Call, rng: random.Random) -> tuple[str, str | None]:
    es = c.lang == "es"
    b = rng.choice(BRANCHES)
    h = HOURS.get(b, "9 to 5 weekdays and 9 to 12 on Saturday")
    if es:
        c.member(f"¿A qué hora abre la sucursal de {b}?")
        c.agent(
            f"La sucursal de {b} abre de lunes a viernes de 9 a 5 y los sábados de 9 a 12. El cajero automático está disponible las 24 horas."
        )
        c.member("Gracias.")
    else:
        c.member(
            rng.choice(
                [
                    f"What time does the {b} branch close today?",
                    f"Is the {b} branch open Saturday?",
                    "Where's the closest branch to Coral Ridge mall?",
                ]
            )
        )
        c.agent(
            f"The {b} branch is open {h}. The drive-through opens 30 minutes earlier, and the ATM is available 24 hours."
        )
        c.member(rng.choice(["Perfect, thanks.", "Great, thank you.", "Okay, that works."]))
    c.guard("Policy gate", "information only: no account access, no verification needed")
    return "resolved", None


def _appointment(c: Call, rng: random.Random) -> tuple[str, str | None]:
    b = rng.choice(BRANCHES)
    topic = rng.choice(
        [
            "a home equity line",
            "refinancing my car",
            "a first-time home buyer mortgage",
            "a small business loan",
            "a personal loan to consolidate cards",
        ]
    )
    day = rng.choice(["Tuesday", "Wednesday", "Thursday", "Friday", "next Monday"])
    hour = rng.choice(["10", "11", "1:30", "2", "3:30", "4"])
    c.member(f"I'd like to meet with a loan officer about {topic}.")
    c.agent("Happy to set that up. Which branch is most convenient, and what day works?")
    c.member(f"{b}, {day} around {hour} if possible.")
    c.guard("Policy gate", "book_meeting allowed (low tier)")
    c.guard("Velocity limit", "1 of 3 bookings per call")
    c.actions.append({"tool": "book_meeting", "status": "booked", "detail": f"{b}, {day} at {hour}: {topic}"})
    c.agent(
        f"You're booked at {b} on {day} at {hour} with a lending specialist. A confirmation is on its way by text and email."
    )
    c.member("Great, thanks.")
    c.flags.append("Appointment")
    return "resolved", None


def _rates(c: Call, rng: random.Random) -> tuple[str, str | None]:
    kind = rng.choice(["auto", "home equity", "used car", "mortgage refinance"])
    c.member(rng.choice([f"What are your {kind} rates right now?", f"I'm shopping {kind} rates, what do you have?"]))
    c.agent(
        f"Rates depend on term and credit, so I don't quote them on the phone. I can have a lending specialist call you "
        f"today with your personalized {kind} rate. Is this number the best one to reach you?"
    )
    if rng.random() < 0.7:
        c.member("Yes, this number is fine.")
        c.guard("Policy gate", "log_lead allowed (low tier); callback number is the calling number")
        c.actions.append(
            {"tool": "log_lead", "status": "logged", "detail": f"{kind.capitalize()} rate callback requested"}
        )
        c.agent("Done. Someone from lending will call you before 5 today.")
        return "resolved", None
    c.member("I'd rather talk to someone now.")
    return c.transfer("asked"), None


def _update_contact(c: Call, rng: random.Random, takeover: bool = False) -> tuple[str, str | None]:
    what = rng.choice(["email", "mailing address", "phone number"])
    c.member(
        rng.choice(
            [
                f"I need to update my {what}.",
                f"I moved, can you change my {what}?",
                f"Please change the {what} on my account.",
            ]
        )
    )
    c.agent("I can help with that. Changes to contact details need verification first.")
    if not c.verify(rng.random() > 0.15, swapped=takeover and rng.random() < 0.5):
        return ("stepup" if not takeover else "risk"), None
    c.guard("Policy gate", f"update_contact allowed for a verified caller ({what})")
    c.actions.append({"tool": "update_contact", "status": "updated", "detail": f"{what.capitalize()} updated"})
    c.agent(f"Your {what} is updated. We'll send a confirmation to your previous contact details too, as a precaution.")
    if takeover:
        amt = round(rng.uniform(3800, 4900), 2)
        c.member(f"Great. Now send a payoff link for {_money(amt)} for my auto loan to the new email.")
        c.risk = 4
        c.signals.append("contact change then payment")
        c.guard(
            "Account-takeover pattern", "contact details changed, then a payment requested on the same call", "block"
        )
        c.flags.append("Fraud stopped")
        return c.transfer(
            "risk",
            "For your security, a team member needs to complete that payment. They'll contact you using the details we had on file before today's change.",
        ), None
    if rng.random() < 0.45:
        c.member("Also, can you waive the fee from last month? I was in the hospital.")
        return c.transfer("complaint"), None
    c.member("Thanks.")
    return "resolved", None


def _fraud_alert(c: Call, rng: random.Random) -> tuple[str, str | None]:
    merchant = rng.choice(MERCHANTS)
    amt = round(rng.uniform(60, 1900), 2)
    c.member("I got a text asking if I made a purchase. I want to check it.")
    c.agent("Thanks for calling about that. I'll verify you, then we can look at it together.")
    if not c.verify(rng.random() > 0.05):
        return "stepup", None
    c.agent(f"The alert was for {_money(amt)} at {merchant}. Did you make that purchase?")
    if rng.random() < 0.55:
        c.member("Yes, that was me.")
        c.agent("Thanks. I've marked it as yours, and the card is ready to use again.")
        c.actions.append({"tool": "create_ticket", "status": "created", "detail": "Fraud alert confirmed by member"})
        return "resolved", None
    c.member("No! That wasn't me.")
    c.agent("I've locked the card. A fraud specialist will take it from here.")
    c.flags.append("Card locked")
    return c.transfer("unsupported"), None


def _login(c: Call, rng: random.Random) -> tuple[str, str | None]:
    c.member(
        rng.choice(
            [
                "I'm locked out of online banking.",
                "The app keeps saying my password is wrong.",
                "I'm not getting the login code on my new phone.",
            ]
        )
    )
    c.agent("I can help. I'll verify you first, then send a password reset link.")
    if not c.verify(rng.random() > 0.1):
        return "stepup", None
    if rng.random() < 0.5:
        c.member("I got a new phone number though, the code goes to the old one.")
        return c.transfer(
            "unsupported", "Changing the phone used for sign-in codes needs a specialist. I'm connecting you now."
        ), None
    ticket = f"CS-{rng.randint(104000, 109999)}"
    c.actions.append(
        {"tool": "create_ticket", "status": "created", "ticket": ticket, "detail": "Online banking reset link sent"}
    )
    c.agent("I've sent a reset link to your email on file. It expires in 15 minutes.")
    c.member("Got it, thank you.")
    return "resolved", None


def _other(c: Call, rng: random.Random) -> tuple[str, str | None]:
    c.member(
        rng.choice(
            [
                "I need a letter for my mortgage company showing my account history.",
                "Can I add my daughter as a joint owner on my savings?",
                "I want to set up a wire to my title company for closing.",
                "I'm going through a hardship and need to talk about my car payment.",
                "How do I set up a CD ladder?",
            ]
        )
    )
    reason = "complaint" if "hardship" in c.turns[-1]["text"] else "unsupported"
    if "wire" in c.turns[-1]["text"]:
        c.guard("Policy gate", "wires are not a voice-agent tool (default deny)", "block")
    return c.transfer(reason), None


def _social_engineering(c: Call, rng: random.Random) -> tuple[str, str | None]:
    amt = round(rng.uniform(1800, 4900), 2)
    other = f"({rng.choice(AREA)}) 555-01{rng.randint(10, 99)}"
    c.member("This is urgent. I'm the owner of the business account and I'm about to board a flight.")
    c.risk += 2
    c.member(f"I don't have time for codes. Text a payment link for {_money(amt)} to my assistant at {other}.")
    c.risk += 3
    c.signals += ["urgency", "authority claim", "new phone number"]
    c.guard(
        "Social-engineering score", f"risk {c.risk} (urgency +2, authority +1, new number +2) ≥ threshold 4", "block"
    )
    c.guard("Payment destination pinned", f"link to {other} refused: links go only to the phone on file", "block")
    c.flags.append("Fraud stopped")
    return c.transfer(
        "risk",
        "I can't send payment links to a new number. A specialist will call you back on the number we have on file.",
    ), None


def _sim_swap(c: Call, rng: random.Random) -> tuple[str, str | None]:
    c.member("I need to change the email on my account and then make a payment.")
    c.agent("I can help with that. I'll need to verify you first.")
    c.verify(swapped=True)
    c.flags.append("Fraud stopped")
    return "risk", None


HANDLERS = {
    "Balance and recent transactions": _balance,
    "Loan payment": _payment,
    "Lost or stolen card": _lost_card,
    "Dispute a card charge": _dispute,
    "Branch hours and locations": _hours,
    "Appointment with a loan officer": _appointment,
    "Auto or home loan rates": _rates,
    "Update address, phone or email": _update_contact,
    "Fraud alert confirmation": _fraud_alert,
    "Online banking login help": _login,
    "Other": _other,
}

SUMMARY = {
    "resolved": "Resolved by the agent",
    "transferred": "Transferred to member services",
    "abandoned": "Caller hung up",
}


def _summary(intent: str, outcome: str, c: Call, reason: str | None) -> str:
    acts = "; ".join(a["detail"] for a in c.actions)
    if "Fraud stopped" in c.flags:
        return f"Possible fraud stopped. {('Actions before the stop: ' + acts + '. ') if acts else ''}{TRANSFER_REASONS.get(reason or 'risk')}; no money moved."
    if outcome == "abandoned":
        return f"Member hung up during {intent.lower()} after {c.t} seconds."
    if outcome == "transferred":
        return f"{intent}. {acts + '. ' if acts else ''}Transferred: {TRANSFER_REASONS[reason].lower()}."
    return f"{intent}. {acts + '.' if acts else 'Answered on the call.'}"


def _members(rng: random.Random, n: int) -> list[dict]:
    seen, out = set(), []
    while len(out) < n:
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "member_no": f"{rng.randint(1000, 9999)}",
                "phone": f"({rng.choice(AREA)}) 555-01{rng.randint(0, 99):02d}",
                "since": rng.randint(1987, 2025),
                "segment": rng.choices(["Everyday", "Plus", "Business"], [0.78, 0.17, 0.05])[0],
            }
        )
    return out


def _times(rng: random.Random, day: date, count: int, last: datetime) -> list[datetime]:
    weights = [
        0.35,
        0.25,
        0.2,
        0.2,
        0.3,
        0.6,
        1.2,
        2.6,
        5.8,
        8.6,
        9.8,
        9.4,
        8.1,
        9.0,
        8.8,
        7.9,
        7.0,
        5.2,
        3.6,
        2.5,
        1.9,
        1.3,
        0.9,
        0.6,
    ]
    end_h = last.hour + last.minute / 60
    out = []
    while len(out) < count:
        h = rng.choices(range(24), weights)[0]
        if h > end_h:
            continue
        ts = datetime(day.year, day.month, day.day, h, rng.randint(0, 59), rng.randint(0, 59))
        if ts <= last:
            out.append(ts)
    return sorted(out, reverse=True)


def build_calls(rng: random.Random, day: date, count: int, last_call: datetime) -> list[dict]:
    members = _members(rng, int(count * 0.86))
    times = _times(rng, day, count, last_call)
    calls = []
    for ts in times:
        member = rng.choice(members)
        roll = rng.random()
        fraud = None
        if roll < 0.010:
            fraud = "social"
        elif roll < 0.018:
            fraud = "simswap"
        elif roll < 0.024:
            fraud = "takeover"
        intent = rng.choices([x[0] for x in INTENT_MIX], [x[1] for x in INTENT_MIX])[0]
        lang = "es" if intent in SPANISH_OK and rng.random() < 0.16 else "en"
        c = Call(rng, member, lang)
        c.opening(state_consent=True)
        if fraud == "social":
            intent = "Loan payment"
            reason, _ = _social_engineering(c, rng)
        elif fraud == "simswap":
            intent = "Update address, phone or email"
            reason, _ = _sim_swap(c, rng)
        elif fraud == "takeover":
            intent = "Update address, phone or email"
            reason, _ = _update_contact(c, rng, takeover=True)
        elif rng.random() < 0.017:
            c.member(rng.choice(["Hello? Hello?", "Uh, hang on one second.", "Representative."]))
            c.system("Caller hung up")
            reason = "abandoned"
        else:
            reason, _ = HANDLERS[intent](c, rng)
            # Intents whose script always resolves still lose some callers to "let me talk to a person".
            if reason == "resolved" and rng.random() < EXTRA_TRANSFER.get(intent, 0):
                c.member(
                    rng.choice(["Actually, can I just talk to a person?", "Can I speak to someone about my account?"])
                )
                reason = c.transfer("asked")
        if reason not in ("resolved", "abandoned") and not any(
            t["who"] == "system" and t["text"].startswith("Transferred") for t in c.turns
        ):
            c.transfer(reason)
        if reason == "abandoned":
            outcome, transfer_reason = "abandoned", None
        elif reason == "resolved":
            outcome, transfer_reason = "resolved", None
            c.agent(
                "Thanks for calling Cypress Harbor. Have a great day."
                if lang == "en"
                else "Gracias por llamar. Que tenga un buen día."
            )
        else:
            outcome, transfer_reason = "transferred", reason
        c.guard("Audit log", f"{len(c.safeguards) + 1} entries, hash chain verified", "pass")
        sentiment = (
            "negative"
            if outcome == "transferred" and transfer_reason in ("complaint", "asked") and rng.random() < 0.6
            else "positive"
            if outcome == "resolved" and rng.random() < 0.55
            else "neutral"
        )
        csat = None
        if outcome != "abandoned" and rng.random() < 0.37:
            csat = rng.choices(
                [5, 4, 3, 2, 1],
                [0.66, 0.2, 0.07, 0.04, 0.03] if outcome == "resolved" else [0.38, 0.27, 0.16, 0.11, 0.08],
            )[0]
        qa = []
        if outcome == "resolved":
            qa.append("First-contact resolution")
        if transfer_reason == "unsupported":
            qa.append("Coverage gap")
        if transfer_reason == "asked" and outcome == "transferred":
            qa.append("Asked for a person")
        if "Fraud stopped" in c.flags:
            qa.append("Fraud review")
        if csat is not None and csat <= 2:
            qa.append("Low satisfaction")
        sid = "CA" + "".join(rng.choice("0123456789abcdef") for _ in range(32))
        calls.append(
            {
                "id": sid,
                "started": ts.isoformat(),
                "duration_s": c.t + rng.randint(4, 12),
                "member": {k: member[k] for k in ("name", "member_no", "phone", "since", "segment")},
                "language": lang,
                "intent": intent,
                "outcome": outcome,
                "transfer_reason": TRANSFER_REASONS.get(transfer_reason) if transfer_reason else None,
                "verification": c.verified,
                "risk_score": c.risk,
                "risk_signals": c.signals,
                "flags": sorted(set(c.flags)),
                "actions": c.actions,
                "sentiment": sentiment,
                "csat": csat,
                "qa_tags": qa,
                "summary": _summary(intent, outcome, c, transfer_reason),
                "transcript": c.turns,
                "safeguards": c.safeguards,
                "agent": AGENT,
                "queue": "Spanish" if lang == "es" else "Member services",
            }
        )
    return calls
