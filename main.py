from bs4 import BeautifulSoup
import requests

url = 'https://joyreactor.cc/'
page = requests.get(url)
print(page.status_code)
filteredNews = []
allNews = []

soup = BeautifulSoup(page.text, "html.parser")
print(soup)