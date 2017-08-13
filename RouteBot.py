import json
import cPickle
import os.path
import Queue
import requests
import signal
import slacker
import threading
import time
import urllib
import urllib2

class Route:

  def __init__(self, route_id, name, route_type, rating, stars, votes, \
      pitches, location, url, small_img, med_img):
    self.route_id = route_id
    self.name = name
    self.route_type = route_type
    self.rating = rating
    self.stars = stars
    self.votes = votes
    self.pitches = pitches
    self.location = location
    self.url = url
    self.small_img = small_img
    self.med_img = med_img


class RouteBot:

  def __init__(self):
    self.LOG_FILE_NAME = "route_bot.log"
    self.SLACK_API_ACCESS_TOKEN = "xoxb-136583720599-9ht3pRDyDHP2I3PmslLElxdu"
    self.MOUNTAIN_PROJECT_KEY = "112474424-ced6163b0575e6059b799211dcd1f4a3"
    self.MP_SEARCH_URL = "https://www.mountainproject.com/scripts/Search?%s"
    self.MP_ROUTE_API_URL = "https://www.mountainproject.com/data"
    self.ROUTE_IDS_FILE = "data/mountain_project_route_ids"
    self.ROUTE_INFO_FILE = "data/mountain_project_route_info"
    self.CHANNEL_ID = "C43EUTGQ0"
    self.GENERAL_CHANNEL = "C3S8D8ZMH"
    self.WORKER_THREADS = 2
    self.MESSAGE_FETCH_INTERVAL_SECS = 1
    self.MAINTENANCE_INTERVAL_SECS = 120

    self.save_flag = False
    self.exit_flag = False

    self.workers = []
    for i in range(self.WORKER_THREADS):
      self.workers.append(threading.Thread(target=self.worker_op))
    self.maintenance_daemon = threading.Thread(target=self.maintenance_op)
    self.logger = threading.Thread(target=self.logger_op)

    self.log_queue = Queue.Queue()

    self.slack_client = slacker.Slacker(self.SLACK_API_ACCESS_TOKEN)
    self.client_lock = threading.Lock()

    self.route_ids = {}
    self.route_info = {}
    self.ids_lock = threading.Lock()
    self.info_lock = threading.Lock()

    self.message_queue = Queue.Queue()
    self.queue_sema = threading.Semaphore(0)
    self.last_timestamp = str(time.time())


  def load_routes(self):
    """
    Load pickled route info into memory
    """
    self.log("Loading route info...")
    if not os.path.isfile(self.ROUTE_IDS_FILE):
      return
    if not os.path.isfile(self.ROUTE_INFO_FILE):
      return
    id_file = open(self.ROUTE_IDS_FILE, "rb")
    info_file = open(self.ROUTE_INFO_FILE, "rb")

    self.ids_lock.acquire()
    self.route_ids = cPickle.load(id_file)
    self.ids_lock.release()

    self.info_lock.acquire()
    self.route_info = cPickle.load(info_file)
    self.info_lcok.release()

    id_file.close()
    info_file.close()


  def save_route_map(self):
    """
    Pickle route info to disk
    """
    self.log("Saving routes...")
    id_file = open(self.ROUTE_IDS_FILE, "wb")
    info_file = open(self.ROUTE_INFO_FILE, "wb")

    self.ids_lock.acquire()
    cPickle.dump(self.route_ids, id_file)
    self.ids_lock.release()

    self.info_lock.acquire()
    cPickle.dump(self.route_info, info_file)
    self.info_lock.release()

    id_file.close()
    info_file.close()


  def add_route(self, route_name, route_id):
    """
    Adds a new route with the given name and id to RouteBot's known routes, then fetches info for it
    """
    standardized = route_name.lower()
    self.log("Adding route %s with id %s" % (standardized, route_id))

    self.ids_lock.acquire()
    self.route_ids[standardized] = route_id
    self.ids_lock.release()

    route_info = self.get_mp_route_info(route_id)
    self.info_lock.acquire()
    self.route_info[route_id] = route_info
    self.info_lock.release()

    self.save_flag = True


  def get_mp_search_link(self, search_terms):
    """
    Returns a link to the mountain project search page for the given search terms
    """
    encoded_terms = urllib.urlencode({"query" : search_terms, "x" : "0", "y" : "0"})
    full_url = self.MP_SEARCH_URL % encoded_terms
    return full_url


  def get_mp_route_info(self, route_id):
    """
    Fetches route info for a given route id from MountainProject's API
    """
    arg_data = {"action" : "getRoutes", "routeIds" : route_id, "key" : self.MOUNTAIN_PROJECT_KEY}
    encoded_args = urllib.urlencode(arg_data)
    full_url = (self.MP_ROUTE_API_URL + "?%s") % encoded_args
    response = requests.get(full_url)
    response = response.json()
    if response["success"] != 1:
      return None
    obj = response["routes"][0]
    return Route(obj["id"], obj["name"], obj["type"], obj["rating"], \
        obj["stars"], obj["starVotes"], obj["pitches"], obj["location"], \
        obj["url"], obj["imgSmall"], obj["imgMed"])


  def read_messages(self):
    self.client_lock.acquire()
    history = self.slack_client.channels.history(channel=self.CHANNEL_ID, oldest=self.last_timestamp)
    self.client_lock.release()
    if not history.successful or len(history.body["messages"]) == 0:
      return
    self.last_timestamp = str(history.body["messages"][0]["ts"])
    for message in history.body["messages"]:
      self.message_queue.put(message)
      self.queue_sema.release()


  def handle_message(self, message):
    print message
    self.client_lock.acquire()
    self.slack_client.chat.post_message(channel=self.CHANNEL_ID, text="Received: " + str(message['text']))
    self.client_lock.release()


  def log(self, message):
    self.log_queue.put(str(time.time()) + ": " + message + "\n")


  def logger_op(self):
    log_file = open(self.LOG_FILE_NAME, "a")
    while True:
      log_file.write(self.log_queue.get())
      if self.exit_flag:
        break
    log_file.close()


  def maintenance_op(self):
    while True:
      if self.save_flag:
        self.save_flag = False
        self.save_routes()
      if self.exit_flag:
        break
      time.sleep(self.MAINTENANCE_INTERVAL_SECS)


  def worker_op(self):
    while True:
      self.queue_sema.acquire()
      if self.exit_flag:
        break
      message = self.message_queue.get(False)
      if message is not None:
        self.handle_message(message)


  def listen(self):
    while True:
      self.read_messages()
      time.sleep(self.MESSAGE_FETCH_INTERVAL_SECS)
      if self.exit_flag:
        break


  def start_threads(self):
    self.logger.start()
    self.maintenance_daemon.start()
    for worker in self.workers:
      worker.start()


  def halt(self):
    self.exit_flag = True
    self.log("Halting RouteBot...")
    for worker in self.workers:
      self.queue_sema.release()
      worker.join()
    self.maintenance_daemon.join()
    self.maintenance_op()
    self.logger.join()


def main():
  route_bot = RouteBot()

  """
  def interrupt_handler(signum, frame):
    route_bot.halt()
    sys.exit()
  signal.signal(signal.SIGINT, interrupt_handler)
  """

  route_bot.load_routes()
  route_bot.start_threads()
  route_bot.listen()


if __name__ == "__main__":
  main()

