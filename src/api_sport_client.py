import requests
from datetime import date

class ApiSportClient:
    def __init__(self, api_key: str, base_url: str = "https://api.api-sport.ru"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, params: dict | None = None):
        url = f"{self.base_url}{path}"
        headers = {
            # если в документации пример с X-API-Key — используй именно его:
            "X-API-Key": self.api_key,
            # или "Authorization": self.api_key,
        }
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_khl_matches_for_date(self, tournament_id: int, dt: date):
        return self._get(
            "/v2/ice-hockey/matches",
            params={
                "tournamentId": tournament_id,
                "date": dt.strftime("%Y-%m-%d"),
            },
        )

    def get_khl_live_matches(self, tournament_id: int):
        return self._get(
            "/v2/ice-hockey/matches",
            params={
                "tournamentId": tournament_id,
                "status": "inprogress",
            },
        )
