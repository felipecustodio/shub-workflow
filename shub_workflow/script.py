"""
Implements common methods for ScrapyCloud scripts.
"""
import os
import abc
import logging
from typing import List

from argparse import ArgumentParser

from scrapinghub import ScrapinghubClient, DuplicateJobError

from .utils import (
    resolve_project_id,
    dash_retry_decorator,
    schedule_script_in_dash,
)


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s')


class BaseScript(abc.ABC):

    name = None  # optional, may be needed for some applications
    flow_id_required = True  # if True, script can only run in the context of a flow_id
    children_tags = None

    def __init__(self):
        self.project_id = None
        self.client = ScrapinghubClient()
        self.args = self.parse_args()

    def set_flow_id(self, args, default=None):
        self._flow_id = args.flow_id or self.get_flowid_from_tags() or default
        if self.flow_id_required:
            assert not self.flow_id_required or self.flow_id, "Could not detect flow_id. Please set with --flow-id."
        if self.flow_id:
            self.add_job_tags(tags=[f'FLOW_ID={self.flow_id}'])

    @property
    def description(self):
        return "You didn't set description for this script. Please set description property accordingly."

    @property
    def flow_id(self):
        return self._flow_id

    def add_argparser_options(self):
        self.argparser.add_argument('--project-id', help='Overrides target project id.', type=int)
        self.argparser.add_argument('--name', help='Script name.')
        self.argparser.add_argument('--flow-id', help='If given, use the given flow id.')
        self.argparser.add_argument('--tag', help='Additional tag added to the scheduled jobs. Can be given multiple times.',
                                    action='append', default=self.children_tags or [])

    def parse_project_id(self, args):
        return args.project_id

    def parse_args(self):
        self.argparser = ArgumentParser(self.description)
        self.add_argparser_options()
        args = self.argparser.parse_args()

        self.project_id = resolve_project_id(self.parse_project_id(args))
        if not self.project_id:
            self.argparser.error('Project id not provided.')

        self.set_flow_id(args)

        self.name = args.name or self.name
        return args

    def get_project(self, project_id=None):
        return self.client.get_project(project_id or self.project_id)

    @dash_retry_decorator
    def get_job_metadata(self, jobid=None, project_id=None):
        """If jobid is None, get own metadata
        """
        jobid = jobid or os.getenv('SHUB_JOBKEY')
        if jobid:
            project = self.get_project(project_id)
            job = project.jobs.get(jobid)
            return job.metadata
        logger.warning('SHUB_JOBKEY not set: not running on ScrapyCloud.')

    @dash_retry_decorator
    def get_job(self, jobid=None):
        """If jobid is None, get own metadata
        """
        jobid = jobid or os.getenv('SHUB_JOBKEY')
        if jobid:
            project_id = jobid.split('/', 1)[0]
            project = self.get_project(project_id)
            return project.jobs.get(jobid)
        logger.warning('SHUB_JOBKEY not set: not running on ScrapyCloud.')

    def get_job_tags(self, jobid=None, project_id=None):
        """If jobid is None, get own tags
        """
        metadata = self.get_job_metadata(jobid, project_id)
        if metadata:
            return dict(metadata.list()).get('tags', [])
        return []

    @staticmethod
    @dash_retry_decorator
    def update_metadata(metadata, data):
        metadata.update(data)

    def add_job_tags(self, jobid=None, project_id=None, tags=None):
        """If jobid is None, add tags to own list of tags.
        """
        if tags:
            update = False
            job_tags = self.get_job_tags(jobid, project_id)
            for tag in tags:
                if tag not in job_tags:
                    if tag.startswith('FLOW_ID='):
                        job_tags.insert(0, tag)
                    else:
                        job_tags.append(tag)
                    update = True
            if update:
                metadata = self.get_job_metadata(jobid, project_id)
                if metadata:
                    self.update_metadata(metadata, {'tags': job_tags})

    def get_flowid_from_tags(self, jobid=None, project_id=None):
        """If jobid is None, get flowid from own tags
        """
        for tag in self.get_job_tags(jobid, project_id):
            if tag.startswith('FLOW_ID='):
                return tag.replace('FLOW_ID=', '')

    def _make_tags(self, tags):
        tags = tags or []
        tags.extend(self.args.tag)
        if self.flow_id:
            tags.append(f'FLOW_ID={self.flow_id}')
        return list(set(tags)) or None

    def schedule_script(self, cmd: List[str], tags=None, project_id=None, **kwargs):
        """
        Schedules an external script
        """
        logger.info('Starting: {}'.format(cmd))
        project = self.get_project(project_id)
        job = schedule_script_in_dash(project, [str(x) for x in cmd], tags=self._make_tags(tags), **kwargs)
        logger.info(f"Scheduled script job {job.key}")
        return job.key

    @dash_retry_decorator
    def schedule_spider(self, spider: str, tags=None, units=None, project_id=None, **spiderargs):
        schedule_kwargs = dict(spider=spider, add_tag=self._make_tags(tags), units=units, **spiderargs)
        logger.info("Scheduling a spider:\n%s", schedule_kwargs)
        try:
            project = self.get_project(project_id)
            job = project.jobs.run(**schedule_kwargs)
            logger.info(f"Scheduled spider job {job.key}")
            return job.key
        except DuplicateJobError as e:
            logger.error(str(e))
        except:
            raise

    @dash_retry_decorator
    def get_jobs(self, project_id=None, **kwargs):
        return self.get_project(project_id).jobs.iter(**kwargs)

    def get_owned_jobs(self, project_id, **kwargs):
        assert self.flow_id, "This job doesn't have a flow id."
        assert 'has_tag' not in kwargs, "Filtering by flow id requires no extra has_tag."
        kwargs['has_tag'] = [f'FLOW_ID={self.flow_id}']
        return self.get_jobs(project_id, **kwargs)

    @dash_retry_decorator
    def is_running(self, jobkey, project_id=None):
        """
        Checks whether a job is running (or pending)
        """
        project = self.get_project(project_id)
        job = project.jobs.get(jobkey)
        if job.metadata.get('state') in ('running', 'pending'):
            return True
        return False

    @dash_retry_decorator
    def is_finished(self, jobkey, project_id=None):
        """
        Checks whether a job is running. if so, return close_reason. Otherwise return None.
        """
        project = self.get_project(project_id)
        job = project.jobs.get(jobkey)
        if job.metadata.get('state') == 'finished':
            return job.metadata.get('close_reason')

    @dash_retry_decorator
    def finish(self, jobid=None, close_reason=None):
        close_reason = close_reason or 'finished'
        jobid = jobid or os.getenv('SHUB_JOBKEY')
        if jobid:
            project_id = jobid.split('/', 1)[0]
            hsp = self.client._hsclient.get_project(project_id)
            hsj = hsp.get_job(jobid)
            hsp.jobq.finish(hsj, close_reason=close_reason)
        else:
            logger.warning('SHUB_JOBKEY not set: not running on ScrapyCloud.')

    @abc.abstractmethod
    def run(self):
        pass
