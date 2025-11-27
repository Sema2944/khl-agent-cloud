from api_sport_client import ApiSportClient

API_SPORT_KEY = "95169de3-6577-4d35-acf9-395edfc18f98"
client = ApiSportClient(API_SPORT_KEY)

# Попробуем запросить список хоккейных турниров — путь может быть другим, см. документацию
response = client._get("/ice-hockey/tournaments")  # или "/ice-hockey/categories"
print(response)
