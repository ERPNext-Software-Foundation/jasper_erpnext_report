from __future__ import unicode_literals
__author__ = 'luissaguas'
import jasperserver.core as jasper
from jasperserver.core.reportingService import ReportingService
from jasperserver.core.ReportExecutionRequest import ReportExecutionRequest
from jasperserver.resource_details import Details
from jasperserver.repo_search import Search
from jasperserver.resource_download import DownloadBinary
from jasperserver.core.exceptions import Unauthorized, NotFound


from jasper_erpnext_report.utils.report import prepareCollectResponse

from frappe import _
import frappe

import logging, json
from io import BytesIO
import inspect

from jasper_erpnext_report.utils.file import get_params
import jasper_erpnext_report.utils.utils as utils

import JasperBase as Jb

_logger = logging.getLogger(frappe.__name__)



def _jasperserver(fn):
	"""
	decorator for jasperserver functions
	"""
	def innerfn(*args, **kwargs):
		newargs = {}
		me = args[0]
		try:
			fnargs, varargs, varkw, defaults = inspect.getargspec(fn)
			for a in fnargs:
				if a in kwargs:
					newargs[a] = kwargs.get(a)
			fn_result = fn(*args, **newargs)
			me.update()
			return fn_result

		except Unauthorized as e:
			_logger.info("************************Decorator _jasperserver error {2} args {0} newargs {1}".format(args, newargs, e))
			me._timeout()
			fn_result = fn(*args, **newargs)
			me.update()
			utils.jaspersession_set_value("last_jasper_session_timeout", frappe.utils.now())
			return fn_result
	return innerfn



class JasperServer(Jb.JasperBase):
	def __init__(self, doc={}):
		self.session = frappe.local.jasper_session = frappe._dict({'session': frappe._dict({})})
		super(JasperServer, self).__init__(doc)
		self.check_session()

	def check_session(self):
		if self.data['data'] and self.data['data'].get('cookie'):
			self.resume_connection()
		else:
			self.login()

	def login(self):
		self.connect()
		if not self.in_jasper_session():
			frappe.throw(_("Jasper Server is down. Please ask the administrator to check jasperserver or change to local report only (you will need pyjnius)."))

	def in_jasper_session(self):
		try:
			session = bool(self.session.session.getSessionId())
		except:
			session = False

		return session

	def resume_connection(self):
		self.session = frappe.local.jasper_session = jasper.session(self.doc.get("jasper_server_url"), resume=True)
		self.session.resume(self.data['data']['cookie'])

	def connect(self):
		if self.user=="Guest":
			return

		try:
			if not self.doc:
				#self.doc = frappe.db.get_value('JasperServerConfig', None, "*", ignore=True, as_dict=True)
				#self.createJasperSession()
				self.get_jasperconfig_from_db()

			self.session = frappe.local.jasper_session = jasper.session(self.doc.jasper_server_url, self.doc.jasper_username, self.doc.jasper_server_password)
			self.update_cookie()

		except Exception as e:
			_logger.info("jasperserver login error {0}")
			frappe.msgprint(_("JasperServer login error, reason: {}".format(e)))

	def _timeout(self):
		try:
			self.login()
		except:
			_logger.info("_login: JasperServerSession Error while timeout and login")

		_logger.info("_timeout JasperServerSession login successfuly {0}".format(self.doc))

	def update_cookie(self):
		self.data['data']['cookie'] = frappe.local.jasper_session.session.getSessionId() if frappe.local.jasper_session.session else {}
		self.update(force_cache=False, force_db=True)

	def get_server_info(self):
		print "session {}".format(self.session.session.getSessionId())
		details = Details(self.session)
		serverInfo = details.serverInfo()
		return serverInfo

	def get_jrxml_from_server(self, uri):
		f = DownloadBinary(self.session, uri)
		f.downloadBinary(file=True)
		return f.getFileContent()

	def import_all_jasper_reports(self, data, force=True):
		reports = self.get_reports_list_from_server(force=force)
		docs = utils.do_doctype_from_jasper(data, reports, force=True)
		utils.import_all_jasper_remote_reports(data, docs, force)

	@_jasperserver
	def get_reports_list_from_server(self, force=False):
		ret = {}
		s = Search(self.session)
		result = s.search(path=self.doc.get("jasper_report_root_path"), type="reportUnit")
		reports = result.getDescriptor().json_descriptor()
		for report in reports:
			ics = []
			d = Details(self.session, report.get("uri"))
			r_details = d.details(expanded=False)
			r_param = r_details.getDescriptor().json_descriptor()
			uri = r_param[0].get('jrxml').get('jrxmlFileReference').get('uri')
			file_content = self.get_jrxml_from_server(uri)
			params = get_params(BytesIO(file_content))
			query = self.get_query_jrxmlFile_from_server(file_content)
			for param in params:
				pname = param.xpath('./@name')
				pclass = param.xpath('./@class')
				ptype = pclass[0].split(".")
				c = len(ptype) - 1
				ics.append({"label":pname[0], "type":ptype[c].lower()})
			updateDate = report.get("updateDate", None)
			if not updateDate:
				updateDate = report.get("creationDate", frappe.utils.now())

			ret[report.get("label")] = {"uri":report.get("uri"), "inputControls": ics, "updateDate": updateDate, "queryString": query}

		return ret

	def run_remote_report_async(self, path, doc, data={}, params=[], async=True, pformat="pdf", ncopies=1):
		resps = self.run_report_async(path, doc, data=data, params=params, async=async, pformat=pformat, ncopies=ncopies)
		cresp = prepareCollectResponse(resps)
		cresp["origin"] = "server"
		return [cresp]

	#run reports with http POST and run async and sync
	def _run_report_async(self, path, doc, data={}, params=[], async=True, pformat="pdf", ncopies=1, for_all_sites=1):
		pram = []
		self.doc = doc
		pram_server = []
		pram_copy_index = -1
		pram_copy_page_index = -1
		for param in params:
			is_copy = param.is_copy.lower()
			p = param.jasper_param_name
			value = ""
			if is_copy == _("is for where clause"):
				#value = data.get('name_ids')
				value = data.get('ids')
				if value:
					a = ["'%s'" % t for t in value]
				else:
					value = utils.get_value_param_for_hook(param)
					if not isinstance(value, basestring):
						a = ["'%s'" % t for t in list(value)]
					else:
						a = list(a)
				value = "where name %s (%s)" % (param.param_expression, ",".join(a))
			elif is_copy == _("Is for copies") and pformat=="pdf":
				#set the number of copies
				#indicate the index of param is for copies
				pram_copy_index = len(pram) - 1 if len(pram) > 0 else 0
			elif is_copy == _("is for page number") and pformat=="pdf":
				pram_copy_page_index = len(pram) - 1 if len(pram) > 0 else 0
			elif is_copy == _("is for server hook"):
				value = data.get('ids')
				if not value:
					#if not data and not entered value then get default
					value = utils.get_value_param_for_hook(param)
				pram_server.append({"name":p, 'value': value, "attrs": param})
				continue
			else:
				#value sent take precedence from value in doctype jasper_param_value
				value = data.get(p) or param.jasper_param_value
			pram.append({"name":p, 'value':[value]})
		resp = []
		res = utils.call_hook_for_param(doc, data, pram_server) if pram_server else []
		for param in res:
			param.pop("attrs")
			print "pvalue {}".format(res)
			pram.append(param)
		copies = [_("Single"), _("Duplicated"), _("Triplicate")]
		for m in range(ncopies):
			if pram_copy_index >= 0:
				pram[pram_copy_index]['value'] = [copies[m]]
			if pram_copy_page_index >= 0:
				pram[pram_copy_page_index]['value'] = [str(m) + _(" of ") + str(ncopies)]
			result = self.run_async(path, pram, pformat=pformat)
			requestId = result.get('requestId')
			reqDbObj = {"data":{"result": result, "last_updated": frappe.utils.now(),'session_expiry': utils.get_expiry_period()}}
			self.insert_jasper_reqid_record(requestId, reqDbObj)
			resp.append({"requestId":requestId, "status": result.get('status')})
		#TODO get reqId for check report state
		return resp

	@_jasperserver
	def run_async(self, path, pram, pformat="pdf"):
		rs = ReportingService(self.session)
		rr = ReportExecutionRequest()
		rr.setReportUnitUri(path)
		rr.setOutputFormat(pformat)
		rr.setParameters(pram)
		rr.setAsync(True)
		try:
			req = rs.newReportExecutionRequest(rr)
			res = req.run().getResult("content")
			print "res from server 2: {}".format(res)
			res = json.loads(res)
			resp = self.prepareResponse(res, res.get("requestId"))
			resp["status"] = res.get("status")
			return resp
		except Exception as e:
			frappe.throw(_("Error in report %s, server error is: %s!!!" % (self.doc.jasper_report_name, e)))

	@_jasperserver
	def polling(self, reqId, reqtime=""):
		res = []
		try:
			rs = ReportingService(self.session)
			rexecreq = rs.reportExecutionRequest(reqId)
			status = rexecreq.status().content
			print "status: {}".format(status)
			status = json.loads(status)
			if status.get("value") == "ready":
				detail = self.reportExecDetail(rexecreq)
				print "report detail: {}".format(detail)
				res = self.prepareResponse(detail, reqId)
				if res.get('status') == "not ready":
					res = self.prepareResponse({}, reqId)
				else:
					res["status"] = "ready"
			elif status.get("value") == "failed":
				frappe.throw(_("Error in report %s, server error is: %s!!!" % (self.doc.jasper_report_name, status['errorDescriptor'].get('message'))))
			else:
				res = self.prepareResponse({}, reqId)
		except NotFound:
			frappe.throw(_("Not Found!!!"))
		return res

	#rexecreq : ReportExecutionRequestBuilder instance
	#check if the report is ready and get the expids for ready reports
	def reportExecDetail(self, rexecreq):
		return rexecreq.executionDetails()

	#rs: ReportingService instance
	#get the report with reqId for the given expId
	@_jasperserver
	def getReport(self, reqId, expId):
		try:
			rs = ReportingService(self.session)
			rexecreq = rs.reportExecutionRequest(reqId, expId)
			report = rexecreq.export().outputResource().getResult("content")
			return report
		except NotFound:
			frappe.throw(_("Not Found!!!"))