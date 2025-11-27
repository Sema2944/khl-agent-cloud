import requests
from datetime import date

class ApiSportClient:
    def __init__(self, api_key: str, base_url: str = "https://api.api-sport.ru/v2"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, params: dict | None = None):
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self.api_key,  # если в доках указано X-API-Key — заменим
        }
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_sports(self):
        return self._get("/sport")

    def get_khl_matches_for_date(self, tournament_id: int, dt: date):
        return self._get(
            "/ice-hockey/matches",
            params={
                "tournament_id": tournament_id,
                "date": dt.strftime("%Y-%m-%d"),
            },
        )

    def get_khl_live_matches(self, tournament_id: int):
        return self._get(
            "/ice-hockey/matches",
            params={
                "tournament_id": tournament_id,
                "status": "inprogress",
            },
        )
