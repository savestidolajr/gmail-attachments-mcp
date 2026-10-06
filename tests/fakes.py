from urllib.parse import urlencode


class FakeGoogle:
    """Stands in for oauth.GoogleLogin. No network."""

    def __init__(self):
        self.email = "a@example.com"
        self.verified = True
        self.refresh = "rt-a"
        self.revoked = []

    def authorization_url(self, state):
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({"state": state})

    def exchange_code(self, code):
        tokens = {"access_token": "google-access"}
        if self.refresh:
            tokens["refresh_token"] = self.refresh
        return tokens

    def email_for(self, access_token):
        return self.email, self.verified

    def revoke(self, refresh_token):
        self.revoked.append(refresh_token)
