
import schedule
import time

def trade():
    print("Bot is trading...")

schedule.every(10).seconds.do(trade)

while True:
    schedule.run_pending()
    time.sleep(1)
