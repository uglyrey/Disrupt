"""Version of the privacy policy that people accept.

Change this value together with app/static/privacy.html and the data-policy-version
attribute in app/static/index.html. A test keeps the three in step. A new version asks
existing users to accept it again on their next visit.
"""

from fastapi import HTTPException

POLICY_VERSION = "2026-10-14"
MARKETING_CONSENT_VERSION = "2026-10-14"


def require_consent(accepted, version):
    """Registration and Telegram sign-in both need an explicit, current acceptance."""
    if accepted is not True:
        raise HTTPException(422, "Чтобы продолжить, дайте согласие на обработку персональных данных")
    if version != POLICY_VERSION:
        raise HTTPException(422, "Политика обработки данных обновилась. Обновите страницу и подтвердите согласие ещё раз")


def set_marketing_consent(user, accepted: bool, source: str, now: float) -> None:
    """Keep this optional choice separate from acceptance of the privacy policy."""
    if accepted:
        user.marketing_consent_at = now
        user.marketing_consent_version = MARKETING_CONSENT_VERSION
        user.marketing_consent_source = source
        user.marketing_consent_revoked_at = None
    else:
        user.marketing_consent_at = None
        user.marketing_consent_version = None
        user.marketing_consent_source = None
        user.marketing_consent_revoked_at = now
