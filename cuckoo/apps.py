# Copyright (C) 2016 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import fnmatch
import logging
import os.path
import random
import requests
import StringIO
import tarfile
import time

from cuckoo.common.colors import bold, red, yellow
from cuckoo.common.config import Config
from cuckoo.common.utils import to_unicode
from cuckoo.core.database import Database
from cuckoo.core.database import TASK_FAILED_PROCESSING, TASK_REPORTED
from cuckoo.core.plugins import RunProcessing, RunSignatures, RunReporting
from cuckoo.misc import cwd, mkdir

log = logging.getLogger(__name__)

URL = "https://github.com/cuckoosandbox/community/archive/%s.tar.gz"

def fetch_community(branch="master", force=False, filepath=None):
    if filepath:
        buf = open(filepath, "rb").read()
    else:
        buf = requests.get(URL % branch).content

    t = tarfile.TarFile.open(fileobj=StringIO.StringIO(buf), mode="r:gz")

    folders = {
        os.path.join("modules", "signatures"): "signatures",
        os.path.join("data", "monitor"): "monitor",
        os.path.join("agent"): "agent",
    }

    members = t.getmembers()

    for tarfolder, outfolder in folders.items():
        mkdir(cwd(outfolder))

        # E.g., "community-master/modules/signatures".
        name_start = "%s/%s" % (members[0].name, tarfolder)
        for member in members:
            if not member.name.startswith(name_start) or \
                    name_start == member.name:
                continue

            filepath = cwd(outfolder, member.name[len(name_start)+1:])
            if member.isdir():
                mkdir(filepath)
                continue

            # TODO Ask for confirmation as we used to do.
            if os.path.exists(filepath) and not force:
                log.info(
                    "Not overwriting file which already exists: %s",
                    member.name
                )
                continue

            if member.issym():
                t.makelink(member, filepath)
                continue

            open(filepath, "wb").write(t.extractfile(member).read())

def enumerate_files(path, pattern):
    """Yields all filepaths from a directory."""
    if os.path.isfile(path):
        yield path
    elif os.path.isdir(path):
        for dirname, dirnames, filenames in os.walk(path):
            for filename in filenames:
                filepath = os.path.join(dirname, filename)

                if os.path.isfile(filepath):
                    if pattern:
                        if fnmatch.fnmatch(filename, pattern):
                            yield to_unicode(filepath)
                    else:
                        yield to_unicode(filepath)

def submit_tasks(target, options, package, custom, owner, timeout, priority,
                 machine, platform, memory, enforce_timeout, clock, tags,
                 remote, pattern, maxcount, is_url, is_baseline, is_shuffle):
    db = Database()

    data = dict(
        package=package or "",
        timeout=timeout,
        options=options,
        priority=priority,
        machine=machine,
        platform=platform,
        memory=memory,
        enforce_timeout=enforce_timeout,
        custom=custom,
        owner=owner,
        tags=tags,
    )

    if is_baseline:
        if remote:
            print "Remote baseline support has not yet been implemented."
            return

        task_id = db.add_baseline(timeout, owner, machine, memory)
        yield "Baseline", machine, task_id
        return

    if is_url:
        for url in target:
            if not remote:
                task_id = db.add_url(to_unicode(url), **data)
                yield "URL", url, task_id
                continue

            data["url"] = to_unicode(url)
            try:
                r = requests.post(
                    "http://%s/tasks/create/url" % remote, data=data
                )
                yield "URL", url, r.json()["task_id"]
            except Exception as e:
                print "%s: unable to submit URL: %s" % (
                    bold(red("Error")), e
                )
    else:
        files = []
        for path in target:
            files.extend(enumerate_files(os.path.abspath(path), pattern))

        if is_shuffle:
            random.shuffle(files)

        for filepath in files:
            if not os.path.getsize(filepath):
                print "%s: sample %s (skipping file)" % (
                    bold(yellow("Empty")), filepath
                )
                continue

            if maxcount is not None:
                if not maxcount:
                    break
                maxcount -= 1

            if not remote:
                task_id = db.add_path(file_path=filepath, **data)
                yield "File", filepath, task_id
                continue

            files = {
                "file": (os.path.basename(filepath), open(filepath, "rb")),
            }

            try:
                r = requests.post(
                    "http://%s/tasks/create/file" % remote,
                    data=data, files=files
                )
                yield "File", filepath, r.json()["task_id"]
            except Exception as e:
                print "%s: unable to submit file: %s" % (
                    bold(red("Error")), e
                )
                continue

def process(target, copy_path, task, cfg):
    results = RunProcessing(task=task).run()
    RunSignatures(results=results).run()
    RunReporting(task=task, results=results).run()

    if cfg.cuckoo.delete_original and os.path.exists(target):
        try:
            os.remove(target)
        except OSError as e:
            log.error(
                "Unable to delete original file at path \"%s\": %s",
                target, e
            )

    if cfg.cuckoo.delete_bin_copy and copy_path and os.path.exists(copy_path):
        try:
            os.remove(copy_path)
        except OSError as e:
            log.error(
                "Unable to delete the copy of the original file at "
                "path \"%s\": %s", copy_path, e
            )

def process_tasks(instance, maxcount):
    count = 0
    cfg = Config()
    db = Database()
    db.connect()

    try:
        while not maxcount or count != maxcount:
            task_id = db.processing_get_task(instance)

            # Wait a small while before trying to fetch a new task.
            if task_id is None:
                time.sleep(1)
                continue

            task = db.view_task(task_id)

            log.info("Task #%d: reporting task", task.id)

            if task.category == "file":
                sample = db.view_sample(task.sample_id)
                copy_path = cwd("storage", "binaries", sample.sha256)
            else:
                copy_path = None

            try:
                process(task.target, copy_path, task.to_dict(), cfg)
                db.set_status(task.id, TASK_REPORTED)
            except Exception as e:
                log.exception("Task #%d: error reporting: %s", task.id, e)
                db.set_status(task.id, TASK_FAILED_PROCESSING)

            count += 1
    except Exception as e:
        log.exception("Caught unknown exception: %s", e)