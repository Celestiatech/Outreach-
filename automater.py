from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from time import sleep
import random
import sys
from urllib.parse import quote
import os

DELAY = 30

def get_user_data_dir():
    """Returns the default user data directory path based on the operating system."""
    if os.name == 'nt':  # Windows
        return os.path.join(os.environ['LOCALAPPDATA'], 'AnirudhBagri', 'WABulker', 'User Data')
    elif os.name == 'posix': 
        if os.uname()[0] == 'Darwin': 
            return os.path.join(os.environ['HOME'], 'Library', 'AnirudhBagri', 'WABulker', 'User Data')
        else: 
            return os.path.join(os.environ['HOME'], '.config', 'anirudhbagri', 'wabulker', 'user_data')
    else:
        raise Exception("Unsupported operating system")

def send_messages(driver, numbers, message):
	for idx, number in enumerate(numbers):
		number = number.strip()
		if number == "":
			continue
		print('\n{}/{} => Sending message to {}.'.format((idx+1), len(numbers), number))
		try:
			url = 'https://web.whatsapp.com/send?phone=' + number + '&text=' + message
			sent = False
			for i in range(3):
				if not sent:
					driver.get(url)
					try:
						click_btn = WebDriverWait(driver, DELAY).until(EC.element_to_be_clickable((By.XPATH, "//button[@aria-label='Send']")))
					except Exception as e:
						print(f"\nFailed to send message to: {number}, retry ({i+1}/3)")
						print("Make sure your phone and computer is connected to the internet.")
						print("If there is an alert, please dismiss it.")
					else:
						sleep(1)
						click_btn.click()
						sent=True
						sleep(3)
						print(f'Message sent to: {number}')
						# Safe pacing between messages to reduce risk of rate limiting
						delay = random.randint(8, 15)
						print(f'Waiting {delay} seconds before next message...')
						sleep(delay)
		except Exception as e:
			print(f'Failed to send message to {number}: {e}')

def print_intro():
	print("\n**********************************************************")
	print("*****                                               ******")
	print("*****  THANK YOU FOR USING WHATSAPP BULK MESSENGER  ******")
	print("*****      This tool was built by Anirudh Bagri     ******")
	print("*****           www.github.com/anirudhbagri         ******")
	print("*****                                               ******")
	print("**********************************************************")

def get_message() -> str:
	f = open("message.txt", "r", encoding="utf-8-sig")
	message = f.read()
	f.close()
	print('\nThis is your message-')
	print(message)
	message = quote(message)
	return message

def get_numbers() -> list:
	numbers = []
	f = open("numbers.txt", "r")
	for line in f.read().splitlines():
		if line.strip() != "":
			numbers.append(line.strip())
	f.close()
	return numbers

def get_driver():
	options = Options()
	options.add_experimental_option("excludeSwitches", ["enable-logging"])
	options.add_argument("--profile-directory=Default")
	options.add_argument("--user-data-dir=" + get_user_data_dir())
	driver = webdriver.Chrome(service=Service(), options=options)
	return driver

def login_whatsapp(driver):
	print('Once your browser opens up sign in to web whatsapp')
	driver.get('https://web.whatsapp.com')
	if sys.stdin.isatty():
		try:
			input("AFTER logging into Whatsapp Web is complete and your chats are visible, press ENTER...")
			return
		except EOFError:
			pass
	print("No interactive terminal detected; waiting 45 seconds for WhatsApp Web login...")
	sleep(45)

def main():
	print_intro()
	message = get_message()
	numbers = get_numbers()
	print(f"\nFound {len(numbers)} numbers in the file.")
	driver = get_driver()
	login_whatsapp(driver)
	send_messages(driver, numbers, message)
	driver.close()
	print("\nAll done! Thanks for using WhatsApp Bulk Messenger.")

if __name__ == "__main__":
	main()
