import requests
from requests.adapters import HTTPAdapter, Retry
import base64
import json
import logging
from couchbase.cluster import Cluster, ClusterOptions, QueryOptions, ClusterTimeoutOptions
from couchbase.auth import PasswordAuthenticator
from datetime import timedelta

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s:%(levelname)s:%(message)s",
    datefmt="%Y-%m-%d %I:%M:%S%p",
)

# URL is correct for PROD, IDM_AUTH_HEADER_VALUE is part of bitwarden/pps/idm_prod entry as a note
IDM_USERNAME="Administrator"
IDM_PASSWORD=""
IDM_AUTH_HEADER_VALUE = f"Basic "+ base64.b64encode(f"{IDM_USERNAME}:{IDM_PASSWORD}".encode("utf-8")).decode("ascii")
IDM_SEARCH_URL = "https://idm.pps.co.za/midpoint/ws/services/scim2/Users/.search"
session = requests.Session()
retries = Retry(total=5, backoff_factor=1, status_forcelist=[ 502, 503, 504 ], method_whitelist=False)
session.mount('https://', HTTPAdapter(max_retries=retries))

# have to be retrieved from Sameer or bitwarden
CB_URL="couchbase://localhost"
CB_USER="Administrator"
CB_PASS=""
CB_BUCKET="icrypto_persistent_ha"

# Change to false to run delete queries. DRY_RUN=true only prints which entries should be deleted.
DRY_RUN=True

# Records returned from couchbase per query
LIMIT = 20

# IDM is available from VPN, for CB probably do port forwarding and run the script localy? or just leave CB_URL as is and run from couchbase VM (if idm.pps.co.za resolves)
timeout_options=ClusterTimeoutOptions(query_timeout=timedelta(seconds=72000))
cb_cluster = Cluster(CB_URL, ClusterOptions(PasswordAuthenticator(CB_USER, CB_PASS), timeout_options=timeout_options))
cb_bucket = cb_cluster.bucket(CB_BUCKET)
logging.info("Connection to Couchbase established")

# record returned by getFromIDBByUserId
# "urn:ietf:params:scim:schemas:extension:custom:2.0:User": {
#                 "givenName": "midPoint",
#                 "familyName": "Administrator",
#                 "saID": "5363652356345754",
#                 "username": "madministrator",
#                 "eligible": false,
#                 "registered": true,
#                 "mfaEnabled": false,
#                 "Enable2Fa": true,
#                 "passwordlessEnabled": false,
#                 "icryptoUserId": "c7506fec-15a0-49cc-992b-567059ba0c7a",
#                 "dataAlarms": "RawType: (raw): XNode(primitive:parser ValueParser(DOMe, {http://example.com/xml/ns/mySchema}dataAlarms: MISSING_SPARKLE_DATA))"
#             }
def getFromIDMByUserId(icryptoUserId):
    try:
        response = session.post(IDM_SEARCH_URL,
                                 json={"filter": "icryptoUserId eq \"{id}\"".replace("{id}", icryptoUserId)},
                                 headers={"Authorization": IDM_AUTH_HEADER_VALUE})
        if response.json()["totalResults"] > 0:
            # return user data found in idm as presented above
            return response.json()["Resources"][0]["urn:ietf:params:scim:schemas:extension:custom:2.0:User"]
        else:
            # Not found in IDM
            return None
    except Exception as e:
        # dumb retry for connectivity exception
        logging.error("Exception for icryptoUserId " + str(icryptoUserId) + ": " + str(e))
        raise

# Sample returned array
# [
#   {
#     "saID": "7606175036083",
#     "userId": "0000fb7a-f47f-4a8a-80a2-d47d4cf50ad4",
#     "username": "yvupvyvhw-hyvswyvyph"
#   },
#   {
#     "saID": "7911145063088",
#     "userId": "0001c28b-0297-42bf-a433-4f2e2851c82e",
#     "username": "yswypypyyl10"
#   }
# ]

def countAllIDMEntriesFromCouchbase():
    sql_query = 'select count(*) from `icrypto_persistent_ha` where meta().id like "idm_%"'
    row_iter = cb_cluster.query(sql_query)
    return row_iter

def getAllIDMEntriesFromCouchbase(limit, offset):
    sql_query = 'select userId, saID, username, REPLACE(meta().id, "idm_", "") as id from `icrypto_persistent_ha` where meta().id like "idm_%" OFFSET ' + str(offset) + ' LIMIT ' + str(limit)
    row_iter = cb_cluster.query(sql_query)
    return row_iter

def deleteIDMEntryFromCouchbase(id):
    if DRY_RUN:
        logging.info("DRY_RUN Id to remove: idm_"+id)
    else:
        sql_query = 'delete from `icrypto_persistent_ha` where meta().id = $1'
        cb_cluster.query(sql_query,QueryOptions(positional_parameters=["idm_"+id]))
        logging.info("Removed id: "+id)


counter = {
    "allIdm": 0,
    "removed": 0,
    "correct": 0
}

counter["allIdm"] = list(countAllIDMEntriesFromCouchbase())[0]["$1"]

offset = 0
rows = getAllIDMEntriesFromCouchbase(LIMIT, offset)
offset += LIMIT
exceptions = 0
while rows:
    try:
        for row in rows:
            # TODO: check if row['userId'] exists? if not then delete from couchbase too? might be edge case if we introduced uuid later than couchbase integration
            idm_record = getFromIDMByUserId(row['id'])
            if idm_record is None:
                # deleting couchbase record because userID not present in IDM --- ORPHAN RECORD
                # this way we delete all orphans that may cause duplicates later
                # and as well we delete one or more of the duplicates, leaving only the correct record that is in fact present in IDM
                # TODO: for first run just LOG instead of delete?
                deleteIDMEntryFromCouchbase(row['id'])
                counter["removed"] += 1
            else:
                # couchbase record found so I think do nothing?
                # We could verify if saID/username match between row and idm_record, but the clash possibility of random UUID is pretty low
                counter["correct"] += 1
        rows = getAllIDMEntriesFromCouchbase(LIMIT, offset)
        offset += LIMIT
        exceptions = 0
    except Exception as e:
        exceptions += 1
        logging.error('LIMIT: {}, OFFSET: {}, exception: {}'.format(str(LIMIT), str(offset), str(e)))
        if exceptions >= 5:
            logging.error("Number of exceptions >= 5, exiting")
            exit()
        rows = getAllIDMEntriesFromCouchbase(LIMIT, offset)

logging.info(json.dumps(counter, sort_keys=True, indent=4))

# lastly, check if any duplicates found in IDM? Both by uniqueness of saID field and username field separately
# mayeb as a manual query
