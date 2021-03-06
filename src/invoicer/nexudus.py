"""
This file is part of nexudus-usaepay-gateway.

nexudus-usaepay-gateway is free software: you can redistribute it and/or
modify it under the terms of the GNU General Public License as published
by the Free Software Foundation, either version 3 of the License, or (at
your option) any later version.

nexudus-usaepay-gateway is distributed in the hope that it will be
useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License along
with nexudus-usaepay-gateway.  If not, see
<https://www.gnu.org/licenses/>.
"""

import logging
import sys
import pprint
import string
import random
import datetime

import requests
import json
from wtforms.validators import ValidationError
from sqlalchemy.orm.exc import MultipleResultsFound

from .. import config
from ..db import models, conn

class MultipleResultsFoundException(Exception):
    """We tried to get a single record but multiple were found."""


def get_records(url_part, payload, single=False):
    """
    Generator which GETs Nexudus records and yields batches of records.

    :param url_part: Nexudus API URL - everything after "/api/".
    :param payload: GET variables to send along with API request
    :param single: True if we should only have a single record. Throws an
                   exception if more than one result is returned.
    :returns: Yields a batch of records (list of dicts).
    """
    url = config.NEXUDUS_API_URL + url_part
    creds = (config.NEXUDUS_EMAIL, config.NEXUDUS_PASS)
    params = payload
    has_next_page = True
    current_page = 0

    while has_next_page:
        current_page += 1
        params["page"] = current_page
        r = requests.get(url, params=params, auth=creds)

        res = r.json()

        if single:
            if int(res["TotalItems"]) != 1:
                raise MultipleResultsFoundException

        has_next_page = res["HasNextPage"]

        yield res["Records"]


def create_record(url_part, payload):
    """
    Send a POST request to Nexudus, creating a single record.

    :param url_part: Nexudus API URL - everything after "/api/".
    :param payload: Variables to send along with API request.
    """
    url = config.NEXUDUS_API_URL + url_part
    creds = (config.NEXUDUS_EMAIL, config.NEXUDUS_PASS)
    params = payload

    r = requests.post(url, data=params, auth=creds)
    return r.json()


def process_onebyone(url_part, callback, payload={}):
    """
    Make a Nexudus GET request and process each single returned record.

    :param url_part: Nexudus API URL - everything after "/api/".
    :param callback: Callback function to process each return record. Callback
                     should take a single argument (dict)
    :param payload: GET variables to send along with API request. Default
                    empty.
    """
    for record_list in get_records(url_part, payload):
        for record in record_list:
            callback(record)


def process_batch(url_part, callback, payload={}):
    """
    Make a Nexudus GET request and process the returned records in batches.

    Default batch size is 25, but can be changed with a payload variable, e.g.
    { "size" : 1000 }

    :param url_part: Nexudus API URL - everything after "/api/".
    :param callback: Callback function to process each return record. Takes a
                     single argument (dict)
    :param payload: GET variables to send along with API request. Default
                    empty.
    """
    for record_list in get_records(url_part, payload):
        callback(record_list)


def get_first(url_part, payload={}):
    """
    Make a Nexudus GET request and return only the first result.

    :param url_part: Nexudus API URL - everything after "/api".
    :param payload: GET variables to send along with API request. Default
                    empty.
    """
    g = get_records(url_part, payload)
    return next(g)[0]


def get_single(url_part, payload={}):
    """
    Make a Nexudus GET request and return only the first result.

    :param url_part: Nexudus API URL - everything after "/api".
    :param payload: GET variables to send along with API request. Default
                    empty.
    """

    g = get_records(url_part, payload, single=True)
    return next(g)[0]


def get_invoice_list():
    """Print a list of unpaid invoices."""
    payload = {
        'CoworkerInvoice_Paid': 'false',
    }

    def callback(r):
        print(r["CoworkerId"])
        print(r["BusinessId"])

    process_onebyone(
        'billing/coworkerinvoices',
        callback,
        payload
    )


def mark_invoice_paid(invoice_id):
    """
    Mark an invoice as paid in Nexudus.

    :param invoice_id: ID of the invoice
    :returns: (result (bool), message)
    """

    get_payload = {
        "CoworkerInvoice_Id" : invoice_id
    }

    try:
        nexudus_invoice = get_single('billing/coworkerinvoices', get_payload)

    except MultipleResultsFoundException as e:
        return (False, str(e))

    # Nexudus requires the first 10 fields here in order for us to do anything
    # with the invoice.

    pp = pprint.PrettyPrinter(indent=4)
    # pp.pprint(nexudus_invoice)

    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

    create_payload = {
        "CoworkerInvoiceId": invoice_id,
        "CoworkerId": nexudus_invoice["CoworkerId"],
        "BusinessId": nexudus_invoice["BusinessId"],
        # "InvoiceNumber": nexudus_invoice["InvoiceNumber"],
        # "BillToName": nexudus_invoice["BillToName"],
        # "BillToAddress": nexudus_invoice["BillToAddress"],
        # "BillToCity": nexudus_invoice["BillToCity"],
        # "BillToPostCode": nexudus_invoice["BillToPostCode"],
        # "BillToCountryId": nexudus_invoice["BillToCountryId"],
        # "CurrencyId": nexudus_invoice["CurrencyId"],
        "Code": code,
        # "PaymentGatewayName": 6,
        "Credit": nexudus_invoice["TotalAmount"],
        "Description": "Automatic USAePay payment"
    }

    # pp.pprint(create_payload)

    r = create_record('billing/coworkerledgerentries', create_payload)
    sys.stdout.flush()

    if r.get("WasSuccessful"):
        return (True, "Successfully updated invoice in Nexudus.")
    else:
        return (False, r.get("Message"))

def add_or_overwrite_invoice(record, db_sess):
    """
    Add a new Invoice record from Nexudus into our database.

    This compares an Invoice record from the Nexudus database with any record
    we have for the same user. If a field from the Nexudus database contradicts
    a field in our database, overwrite our field (favoring Nexudus as the
    primary source of user data).

    These two nearly-identical callback functions are not very DRY. Consider
    finding a way to generalize them later.

    :param record: Invoice dict from Nexudus API call
    :param db_sess: DB Session
    """

    # First, determine if the corresponding member is set to process
    # automatically. If not, we will ignore this invoice; just short-circuit.
    try:
        corresponding_member = db_sess.query(models.Member).\
            filter_by(nexudus_user_id=record["CoworkerId"]).one_or_none()

        if corresponding_member and not corresponding_member.process_automatically:
            return

    except MultipleResultsFound as e:
        logger = logging.getLogger('invoicer_db')
        logger.warn('Consistency warning: Multiple users in database ' +
                    'with Nexudus ID ' + str(record["CoworkerId"]) + ".")

    try:
        invoice_to_add = db_sess.query(models.Invoice).\
            filter_by(nexudus_invoice_id=record["Id"]).one_or_none()
    except MultipleResultsFound as e:
        logger = logging.getLogger('invoicer_db')
        logger.warn('Consistency warning: Multiple invoices in database '
                    'session with Nexudus ID ' + str(record["Id"]) +
                    '. Removing all copies of this user and re-syncing.')
        db_sess.query(models.Invoice).\
            filter_by(nexudus_invoice_id=record["Id"]).delete()
        invoice_to_add = None

    # Add the invoice if it's not already in the DB. If it's already in, we
    # have nothing to do here.
    if not invoice_to_add:
        invoice_to_add = models.Invoice()
        invoice_to_add.nexudus_invoice_id = record["Id"]
        invoice_to_add.nexudus_user_id = record["CoworkerId"]
        invoice_to_add.nexudus_space_id = record["BusinessId"]
        invoice_to_add.time_created = datetime.datetime.now()
        invoice_to_add.amount = float(record["TotalAmount"])
        invoice_to_add.finalized = False
        invoice_to_add.txn_id = None
        invoice_to_add.txn_status = "Not processed"
        invoice_to_add.txn_statuscode = None

        db_sess.add(invoice_to_add)
        ajax_logger = logging.getLogger('invoicer_ajax')
        ajax_logger.debug(
            "    New invoice found for Nexudus user ID " +
            str(invoice_to_add.nexudus_user_id) +
            "."
        )

        db_sess.commit()


def add_or_overwrite_member(record, db_sess):
    """
    Add a new Member record from Nexudus to our database.

    This Compares a Member record from the Nexudus database with any record we
    have for the same user. If a field from the Nexudus database contradicts a
    field in our database, overwrite our field (favoring Nexudus as the primary
    source of user data).

    :param record: Member dict from Nexudus API call
    :param db_sess: DB Session
    """
    try:
        member_to_add = db_sess.query(models.Member).\
            filter_by(nexudus_user_id=record["Id"]).one_or_none()
    except MultipleResultsFound as e:
        logger = logging.getLogger('invoicer_db')
        logger.warn('Consistency warning: Multiple users in database session '
                    'with Nexudus ID ' + str(record["Id"]) + '. Removing all '
                    'copies of this user and re-syncing.')
        db_sess.query(models.Member).\
            filter_by(nexudus_user_id=record["Id"]).delete()
        member_to_add = None

    # Flag for later use
    already_stored = True

    if not member_to_add:
        member_to_add = models.Member()
        db_sess.add(member_to_add)
        already_stored = False

    member_to_add.nexudus_user_id = record["Id"]
    member_to_add.fullname = record["FullName"]
    member_to_add.billing_name = record["BillingName"] or record["FullName"]
    member_to_add.email = record["Email"]

    # TODO Update to use custom fields for ACH data should go here. Will need
    # to replace these dictionary keys with Custom1 and Custom2 or whatever
    # custom fields are actually used.
    member_to_add.routing_number = record["BankBranch"]
    member_to_add.account_number = record["BankAccount"]


    def nstrip(s):
        """Return stripped string or None"""
        if s:
            return s.strip()
        else:
            return None

    # If the ACH info looks populated in Nexudus, we'll consider setting this
    # user's invoices to be processed automatically.
    if nstrip(member_to_add.routing_number)\
            and nstrip(member_to_add.account_number):

        # If we've already stored the user, we want to save our earlier
        # preference for automatic processing.
        if not already_stored:
            member_to_add.process_automatically = config.PROCESS_AUTOMATICALLY
    else:
        member_to_add.process_automatically = False

    # Committing each user separately is slower, but a much clearer way to
    # issue updates than the alternatives. Since we're not working with
    # hundreds of thousands of rows at a time, I think this is fine.
    flask_logger = logging.getLogger('flask.app')
    flask_logger.debug("Syncing " + member_to_add.email + " ...")
    db_sess.commit()


def sync_table(sm, nexudus_space_id, sync_callback, payload, url_part, business_var=None):
    """
    Update a local table using records from the Nexudus database.

    :param sm: SQLAlchemy SessionMaker
    :param nexudus_space_id: ID of the Nexudus space we are currently
                             processing records for.
    :param sync_callback: The "add_or_overwrite" callback that is used to
        synchronize individual records
    :param payload: GET payload dict for API request
    :param url_part: Nexudus API URL, e.g. 'spaces/coworkers'
    :param business_var: Nexudus API variable to cross-reference with the
        Space, e.g. CoworkerInvoice_Business or Coworker_InvoicingBusiness
    """
    db_sess = sm()

    def callback(records):
        for record in records:
            try:
                sync_callback(record, db_sess)
            except KeyError as e:
                logger = logging.getLogger('invoicer_db')
                logger.log(logging.ERROR, 'Attempted to access an invalid key for a Nexudus record! Error: ' + str(e))

    # It is important that we only grab coworkers from the spaces we actually
    # want to manage. If we don't do this, coworkers will be pulled from all
    # spaces that this account has access to.

    payload[business_var] = nexudus_space_id
    process_batch(url_part, callback, payload)


def sync_member_table(sm, nexudus_space_id):
    """
    Fill local Member table with records from the Nexudus database.

    :param sm: Database sessionmaker
    :param nexudus_space_id: ID of the Nexudus space we are currently
                             processing members for.
    """
    payload = {
        'Coworker_Active': 'true',
        'size': 100
    }

    sync_table(
        sm,
        nexudus_space_id,
        add_or_overwrite_member,
        payload,
        'spaces/coworkers',
        'Coworker_InvoicingBusiness',
    )


def sync_invoice_table(sm, nexudus_space_id):
    """
    Fill local Invoice table with records from the Nexudus database.

    :param sm: Database sessionmaker
    :param nexudus_space_id: ID of the Nexudus space we are currently
                             processing members for.
    """
    # Only pull unpaid invoices.
    payload = {
        'CoworkerInvoice_Paid': 'false',
        'size': 100
    }

    sync_table(
        sm,
        nexudus_space_id,
        add_or_overwrite_invoice,
        payload,
        'billing/coworkerinvoices',
        'CoworkerInvoice_Business',
    )
