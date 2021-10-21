"""
Base script class for spiders crawl managers.
"""
import abc
import json
import logging
from random import shuffle

from shub_workflow.base import WorkFlowManager


_LOG = logging.getLogger(__name__)
_LOG.setLevel(logging.INFO)


class CrawlManager(WorkFlowManager):
    """
    Schedules a single spider job. If loop mode is enabled, it will shutdown only after the scheduled spider
    finished. Close reason of the manager will be inherited from spider one.
    """

    spider = None

    def __init__(self):
        super().__init__()
        self._bad_outcomes = {}

        # running jobs represents the state of a crawl manager.
        self._running_job_keys = []

    @property
    def description(self):
        return self.__doc__

    def add_argparser_options(self):
        super().add_argparser_options()
        if self.spider is None:
            self.argparser.add_argument("spider", help="Spider name")
        self.argparser.add_argument("--spider-args", help="Spider arguments dict in json format", default="{}")
        self.argparser.add_argument("--job-settings", help="Job settings dict in json format", default="{}")
        self.argparser.add_argument("--units", help="Set number of ScrapyCloud units for each job", type=int)

    def parse_args(self):
        args = super().parse_args()
        if self.spider is None:
            self.spider = args.spider
        return args

    def get_spider_args(self, override=None):
        spider_args = json.loads(self.args.spider_args)
        if override:
            spider_args.update(override)
        return spider_args

    def get_job_settings(self, override=None):
        job_settings = json.loads(self.args.job_settings)
        if override:
            job_settings.update(override)
        return job_settings

    def schedule_spider(self, spider=None, spider_args_override=None, job_settings_override=None):
        spider = spider or self.spider
        spider_args = self.get_spider_args(spider_args_override)
        job_settings = self.get_job_settings(job_settings_override)
        spider_args["job_settings"] = job_settings
        self._running_job_keys.append(super().schedule_spider(spider, units=self.args.units, **spider_args))

    def check_running_jobs(self):
        outcomes = {}
        running_job_keys = list(self._running_job_keys)
        shuffle(running_job_keys)
        for jobkey in running_job_keys:
            outcome = self.is_finished(jobkey)
            if outcome is None:
                _LOG.info(f"Job {jobkey} still running.")
                # if a job is still running, don't waste unneeded requests to get status of other jobs.
                # this is particularly important on crawl managers that handle hundreds of jobs in parallel.
                break
            _LOG.info(f"Job {jobkey} finished with outcome {outcome}.")
            self._running_job_keys.remove(jobkey)
            if outcome in self.base_failed_outcomes:
                self._bad_outcomes[jobkey] = outcome
            outcomes[jobkey] = outcome
        return outcomes

    def workflow_loop(self):
        outcomes = self.check_running_jobs()
        if outcomes:
            return False
        elif not self._running_job_keys:
            self.schedule_spider()
        return True

    def resume_workflow(self):
        for job in self.get_owned_jobs(spider=self.spider, state=["running", "pending"]):
            key = job["key"]
            self._running_job_keys.append(key)
            _LOG.info(f"added running job {key}")

    def on_close(self):
        job = self.get_job()
        if job:
            close_reason = None
            for outcome in self._bad_outcomes.values():
                close_reason = outcome
                break
            self.finish(close_reason=close_reason)


class PeriodicCrawlManager(CrawlManager):
    """
    Schedule a spider periodically, waiting for the previous job to finish before scheduling it again with same
    parameters. Don't forget to set loop mode.
    """

    def workflow_loop(self):
        self.check_running_jobs()
        if not self._running_job_keys:
            self.schedule_spider()
        return True

    def on_close(self):
        pass


class GeneratorCrawlManager(PeriodicCrawlManager):
    """
    Schedule a spider periodically, each time with different parameters yielded by a generator, until stop.
    Number of simultaneos spider jobs will be limited by max running jobs (see WorkFlowManager).
    Instructions:
    - Override set_parameters_gen() method. It must implement a generator of dictionaries, each one being
      the spider arguments (argument name: argument value) passed to the spider on each successive job.
    - Don't forget to set loop mode and max jobs (which defaults to infinite).
    """

    def __init__(self):
        super().__init__()
        self.parameters_gen = self.set_parameters_gen()

    @abc.abstractmethod
    def set_parameters_gen(self):
        for i in []:
            yield i

    def workflow_loop(self):
        self.check_running_jobs()
        if len(self._running_job_keys) < self.max_running_jobs:
            try:
                next_params = next(self.parameters_gen)
                spider = next_params.pop("spider", None)
                self.schedule_spider(spider=spider, spider_args_override=next_params)
            except StopIteration:
                if not self._running_job_keys:
                    return False
        return True
