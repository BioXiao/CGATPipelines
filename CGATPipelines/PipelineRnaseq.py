"""
=====================================
Rnaseq.py - Tools for RNAseq analysis
=====================================

:Author: Andreas Heger
:Release: $Id$
:Date: |today|
:Tags: Python

Purpose
-------

Pipeline components - GO analysis

Tasks related to gene set GO analysis.

Usage
-----

Type::

   python <script_name>.py --help

for command line help.


Requirements:

* HiddenMarkov >= 1.8.0
* cufflinks >= 2.2.1
* MASS >= 7.3.34
* RColorBrewer >= 1.0.5
* featureCounts >= 1.4.3
* samtools >= 1.1

"""

import CGAT.Experiment as E
import CGAT.CSV as CSV

import os
import shutil
import itertools
import glob
import collections
import re
import math
import numpy
import sqlite3

import CGAT.GTF as GTF
import CGAT.BamTools as BamTools
import CGAT.IOTools as IOTools
import CGAT.Database as Database
import CGAT.Expression as Expression
from rpy2.robjects import r as R
import rpy2.robjects as ro
import rpy2.rinterface as ri
import CGATPipelines.Pipeline as P

# AH: commented as I thought we wanted to avoid to
# enable this automatically due to unwanted side
# effects in other modules.
# import rpy2.robjects.numpy2ri
# rpy2.robjects.numpy2ri.activate()

# levels of cuffdiff analysis
# (no promotor and splice -> no lfold column)
CUFFDIFF_LEVELS = ("gene", "cds", "isoform", "tss")

# should be set in calling module
PARAMS = {}

#############################################################
#############################################################
#############################################################
# UTR estimation
#############################################################
Utr = collections.namedtuple("Utr", "old new max status")


def buildUTRExtension(infile, outfile):
    '''build new utrs by building and fitting an HMM
    to reads upstream and downstream of known genes.

    Works on output of buildGeneLevelReadExtension.

    Known problems

    * the size of the extension is limited by the window size

    * introns within UTRs are ignored.

    * UTR extension might be underestimated for highly expressed genes
      as relative read counts drop off quickly, even though there is
      a good amount of reads still present in the UTR.

    The model

    The model is a three-state model::

        UTR --|--> notUTR --|--> otherTranscript --|
          ^---|      ^------|              ^-------|
                     ^-----------------------------|

    The chain starts in UTR and ends in notUTr or otherTranscript.

    The otherTranscript state models peaks of within the upstream/
    downstream region of a gene. These peaks might correspond to
    additional exons or unknown transcripts. Without this state,
    the UTR might be artificially extend to include these peaks.

    Emissions are modelled with beta distributions. These
    distributions permit both bimodal (UTR) and unimodal (notUTR)
    distribution of counts.

    Parameter estimation

    Parameters are derived from known UTRs within full length
    territories.

    Transitions and emissions for the otherTranscript state
    are set heuristically:

       * low probabibily for remaining in state "otherTranscript".
           * these transcripts should be short.

       * emissions biased towards high counts - only strong signals
           will be considered.

       * these could be estimated from known UTRs, but I am worried
           UTR extensions then will be diluted.


    Alternatives

    The method could be improved.

        * base level resolution?
            * longer chains result in more data and longer running times.
            * the averaging in windows smoothes the data, which might have
                a beneficial effect.

        * raw counts instead of scaled counts?
            * better model, as highly expressed genes should give more
                confident predictions.

    '''

    # the bin size , see gtf2table - can be cleaned from column names
    # or better set as options in .ini file
    binsize = 100
    territory_size = 15000

    # read gene coordinates
    geneinfos = {}
    for x in CSV.DictReader(IOTools.openFile(infile), dialect='excel-tab'):
        contig, strand, start, end = x['contig'], x[
            'strand'], int(x['start']), int(x['end'])
        geneinfos[x['gene_id']] = (contig, strand,
                                   start, end)

    infiles = [infile + ".readextension_upstream_sense.tsv.gz",
               infile + ".readextension_downstream_sense.tsv.gz"]

    outdir = os.path.join(PARAMS["exportdir"], "utr_extension")

    R('''suppressMessages(library(RColorBrewer))''')
    R('''suppressMessages(library(MASS))''')
    R('''suppressMessages(library(HiddenMarkov))''')

    # for upstream, downstream
    upstream_utrs, downstream_utrs = {}, {}

    all_genes = set()

    for filename, new_utrs in zip(infiles, (upstream_utrs, downstream_utrs)):

        E.info("processing %s" % filename)

        parts = os.path.basename(filename).split(".")

        data = R(
            '''data = read.table(gzfile( "%(filename)s"), header=TRUE,
            fill=TRUE, row.names=1)''' % locals())

        ##########################################
        ##########################################
        ##########################################
        # estimation
        ##########################################
        # take only those with a 'complete' territory
        R('''d = data[-which( apply( data,1,function(x)any(is.na(x)))),]''')
        # save UTR
        R('''utrs = d$utr''')
        # remove length and utr column
        R('''d = d[-c(1,2)]''')
        # remove those which are completely empty, logtransform or scale data
        # and export
        R('''lraw = log10(
        d[-which(apply(d, 1, function(x)all(x==0))),] + 1)''')

        utrs = R('''utrs = utrs[-which( apply(d,1,function(x)all(x==0)))]''')
        scaled = R(
            '''lscaled = t(scale(t(lraw), center=FALSE,
            scale=apply(lraw,1,max)))''')
        exons = R('''lraw[,1]''')

        #######################################################
        #######################################################
        #######################################################
        # do the estimation:
        E.debug("estimation: utrs=%i, exons=%i, vals=%i, dim=%s" %
                (len(utrs), len(exons), len(scaled), R.dim(scaled)))
        # counts within and outside UTRs
        within_utr, outside_utr, otherTranscript = [], [], []
        # number of transitions between utrs
        transitions = numpy.zeros((3, 3), numpy.int)

        for x in xrange(len(utrs)):
            utr, exon = utrs[x], exons[x]

            # only consider genes with expression coverage
            # note: expression level is logscaled here, 10^1 = 10
            if exon < 0.1:
                continue

            # first row is column names, so x + 1
            values = list(scaled.rx(x + 1, True))

            utr_bins = utr // binsize
            nonutr_bins = (territory_size - utr) // binsize

            # build transition matrix
            transitions[0][0] += utr_bins
            transitions[0][1] += 1
            transitions[1][1] += nonutr_bins

            outside_utr.extend([x for x in values[utr_bins:] if x <= 0.5])

            # ignore exon and zero counts
            within_utr.extend([x for x in values[1:utr_bins] if x > 0.1])

            # add only high counts to otherTranscript emissions
            otherTranscript.extend([x for x in values[utr_bins:] if x > 0.5])

        # estimation for
        # 5% chance of transiting to otherTranscript
        transitions[1][2] = transitions[1][1] * 0.05
        # 10% chance of remaining in otherTranscript
        transitions[2][1] = 900
        transitions[2][2] = 100

        E.info("counting: (n,mean): within utr=%i,%f, "
               "outside utr=%i,%f, otherTranscript=%i,%f" %
               (len(within_utr), numpy.mean(within_utr),
                len(outside_utr), numpy.mean(outside_utr),
                len(otherTranscript), numpy.mean(otherTranscript)))

        ro.globalenv['transitions'] = R.matrix(transitions, nrow=3, ncol=3)
        R('''transitions = transitions / rowSums( transitions )''')
        ro.globalenv['within_utr'] = ro.FloatVector(within_utr[:10000])
        ro.globalenv['outside_utr'] = ro.FloatVector(outside_utr[:10000])
        ro.globalenv['otherTranscript'] = ro.FloatVector(
            otherTranscript[:10000])

        # estimate beta distribution parameters
        R('''doFit = function( data ) {
                   data[data == 0] = data[data == 0] + 0.001
                   data[data == 1] = data[data == 1] - 0.001
                   f = fitdistr( data, dbeta, list( shape1=0.5, shape2=0.5 ) )
                   return (f) }''')

        fit_within_utr = R(
            '''fit_within_utr = suppressMessages(doFit( within_utr))''')
        fit_outside_utr = R(
            '''fit_outside_utr = suppressMessages(doFit( outside_utr))''')
        fit_other = R(
            '''fit_otherTranscript = suppressMessages(
            doFit(otherTranscript))''')

        within_a, within_b = list(fit_within_utr.rx("estimate"))[0]
        outside_a, outside_b = list(fit_outside_utr.rx("estimate"))[0]
        other_a, other_b = list(fit_other.rx("estimate"))[0]

        E.info("beta estimates: within_utr=%f,%f outside=%f,%f, other=%f,%f" %
               (within_a, within_b, outside_a, outside_b, other_a, other_b))

        fn = ".".join((parts[0], parts[4], "fit", "png"))
        outfilename = os.path.join(outdir, fn)
        R.png(outfilename, height=1000, width=1000)

        R('''par(mfrow=c(3,1))''')
        R('''x=seq(0,1,0.02)''')
        R('''hist( within_utr, 50, col=rgb( 0,0,1,0.2) )''')
        R('''par(new=TRUE)''')
        R('''plot(x, dbeta(x, fit_within_utr$estimate['shape1'],
        fit_within_utr$estimate['shape2']), type='l', col='blue')''')

        R('''hist( outside_utr, 50, col=rgb( 1,0,0,0.2 ) )''')
        R('''par(new=TRUE)''')
        R('''plot( x, dbeta( x, fit_outside_utr$estimate['shape1'],
        fit_outside_utr$estimate['shape2']), type='l', col='red')''')

        R('''hist( otherTranscript, 50, col=rgb( 0,1,0,0.2 ) )''')
        R('''par(new=TRUE)''')
        R('''plot( x, dbeta( x, fit_otherTranscript$estimate['shape1'],
        fit_otherTranscript$estimate['shape2']), type='l', col='green')''')
        R['dev.off']()

        #####################################################
        #####################################################
        #####################################################
        # build hmm
        # state 1 = UTR
        # state 2 = notUTR
        # state 3 = other transcript
        p = R('''betaparams = list( shape1=c(fit_within_utr$estimate['shape1'],
        fit_outside_utr$estimate['shape1'],
        fit_otherTranscript$estimate['shape1']),
        shape2=c(fit_within_utr$estimate['shape2'],
        fit_outside_utr$estimate['shape2'],
        fit_otherTranscript$estimate['shape2'])) ''')
        R('''hmm = dthmm(NULL, transitions, c(1,0,0), "beta", betaparams )''')

        E.info("fitting starts")
        #####################################################
        #####################################################
        #####################################################
        # fit to every sequence
        genes = R('''rownames(data)''')
        all_genes.update(set(genes))
        utrs = R('''data$utr''')
        exons = R('''data$exon''')
        nseqs = len(utrs)

        counter = E.Counter()

        for idx in xrange(len(utrs)):

            gene_id = genes[idx]

            old_utr = utrs[idx]

            if idx % 100 == 0:
                E.debug("processing gene %i/%i" % (idx, len(utrs)))

            counter.input += 1

            # do not predict if terminal exon not expressed
            if exons[idx] < 1:
                counter.skipped_notexpressed += 1
                new_utrs[gene_id] = Utr._make(
                    (old_utr, None, None, "notexpressed"))
                continue

            R('''obs = data[%i,][-c(1,2)]''' % (idx + 1))
            # remove na
            obs = R('''obs = obs[!is.na(obs)]''')
            if len(obs) <= 1 or max(obs) == 0:
                new_utrs[gene_id] = Utr._make(
                    (old_utr, None, None, "no observations"))
                continue

            # normalize
            R('''obs = obs / max(obs)''')
            # add small epsilon to 0 and 1 values
            R('''obs[obs==0] = obs[obs==0] + 0.001 ''')
            R('''obs[obs==1] = obs[obs==1] - 0.001 ''')
            R('''hmm$x = obs''')

            states = None
            try:
                states = list(R('''states = Viterbi( hmm )'''))
            except ri.RRuntimeError, msg:
                counter.skipped_error += 1
                new_utrs[gene_id] = Utr._make((old_utr, None, None, "fail"))
                continue

            max_utr = binsize * (len(states) - 1)

            # subtract 1 for last exon
            try:
                new_utr = binsize * (states.index(2) - 1)
                new_utrs[gene_id] = Utr._make(
                    (old_utr, new_utr, max_utr, "ok"))
                counter.success += 1
            except ValueError:
                new_utrs[gene_id] = Utr._make(
                    (old_utr, max_utr, max_utr, "max"))
                counter.maxutr += 1

    E.info("fitting: %s" % str(counter))

    outf = IOTools.openFile(outfile, "w")

    outf.write("\t".join(
        ["gene_id", "contig", "strand", "status5", "status3"] +
        ["%s_%s_%s" % (x, y, z) for x, y, z in itertools.product(
            ("old", "new", "max"),
            ("5utr", "3utr"),
            ("length", "start", "end"))]) + "\n")

    def _write(coords, strand):

        start5, end5, start3, end3 = coords
        if strand == "-":
            start5, end5, start3, end3 = start3, end3, start5, end5

        if start5 is None:
            start5, end5, l5 = "", "", ""
        else:
            l5 = end5 - start5

        if start3 is None:
            start3, end3, l3 = "", "", ""
        else:
            l3 = end3 - start3

        return "\t".join(map(str, (l5, start5, end5,
                                   l3, start3, end3)))

    def _buildCoords(upstream, downstream, start, end):

        r = []
        if upstream:
            start5, end5 = start - upstream, start
        else:
            start5, end5 = None, None
        if downstream:
            start3, end3 = end, end + downstream
        else:
            start3, end3 = None, None

        return start5, end5, start3, end3

    for gene_id in all_genes:

        contig, strand, start, end = geneinfos[gene_id]

        outf.write("%s\t%s\t%s" % (gene_id, contig, strand))

        if gene_id in upstream_utrs:
            upstream = upstream_utrs[gene_id]
        else:
            upstream = Utr._make((None, None, None, "missing"))
        if gene_id in downstream_utrs:
            downstream = downstream_utrs[gene_id]
        else:
            downstream = Utr._make((None, None, None, "missing"))

        if strand == "-":
            upstream, downstream = downstream, upstream

        # output prediction status
        outf.write("\t%s\t%s" % (upstream.status, downstream.status))

        # build upstream/downstream coordinates
        old_coordinates = _buildCoords(
            upstream.old, downstream.old, start, end)
        new_coordinates = _buildCoords(
            upstream.new, downstream.new, start, end)

        # reconciled = take maximum extension of UTR
        max_coordinates = []
        # note that None counts as 0 in min/max.
        for i, d in enumerate(zip(old_coordinates, new_coordinates)):
            if i % 2 == 0:
                v = [z for z in d if z is not None]
                if v:
                    max_coordinates.append(min(v))
                else:
                    max_coordinates.append(None)
            else:
                max_coordinates.append(max(d))

        # convert to 5'/3' coordinates
        outf.write("\t%s\t%s\t%s\n" % (_write(old_coordinates, strand),
                                       _write(new_coordinates, strand),
                                       _write(max_coordinates, strand)))

    outf.close()


def plotGeneLevelReadExtension(infile, outfile):
    '''plot reads extending beyond last exon.'''

    infiles = glob.glob(infile + ".*.tsv.gz")

    outdir = os.path.join(PARAMS["exportdir"], "utr_extension")

    R('''suppressMessages(library(RColorBrewer))''')
    R('''suppressMessages(library(MASS))''')
    R('''suppressMessages(library(HiddenMarkov))''')

    # the bin size , see gtf2table - could be cleaned from column names
    binsize = 100
    territory_size = 15000

    for filename in infiles:

        E.info("processing %s" % filename)

        parts = os.path.basename(filename).split(".")

        data = R(
            '''data = read.table(gzfile("%(filename)s"),
            header=TRUE, fill=TRUE, row.names=1)''' % locals())

        ##########################################
        ##########################################
        ##########################################
        # estimation
        ##########################################
        # take only those with a 'complete' territory
        R('''d = data[-which( apply( data,1,function(x)any(is.na(x)))),]''')
        # save UTR
        R('''utrs = d$utr''')
        # remove length and utr column
        R('''d = d[-c(1,2)]''')
        # remove those which are completely empty, logtransform or scale data
        # and export
        R('''lraw = log10(d[-which( apply(d,1,function(x)all(x==0))),] + 1)''')

        utrs = R('''utrs = utrs[-which( apply(d,1,function(x)all(x==0)))]''')
        scaled = R(
            '''lscaled = t(scale(t(lraw), center=FALSE,
            scale=apply(lraw,1,max)))''')
        exons = R('''lraw[,1]''')

        if len(utrs) == 0:
            E.warn("no data for %s" % filename)
            continue

        #######################################################
        #######################################################
        #######################################################
        R('''myplot = function( reads, utrs, ... ) {
           oreads = t(data.matrix( reads )[order(utrs), ] )
           outrs = utrs[order(utrs)]
           image( 1:nrow(oreads), 1:ncol(oreads), oreads ,
                  xlab = "", ylab = "",
                  col=brewer.pal(9,"Greens"),
                  axes=FALSE)
           # axis(BELOW<-1, at=1:nrow(oreads), labels=rownames(oreads),
           # cex.axis=0.7)
           par(new=TRUE)
           plot( outrs, 1:length(outrs), yaxs="i", xaxs="i",
                 ylab="genes", xlab="len(utr) / bp",
                 type="S",
                 xlim=c(0,nrow(oreads)*%(binsize)i))
        }''' % locals())

        fn = ".".join((parts[0], parts[4], "raw", "png"))
        outfilename = os.path.join(outdir, fn)

        R.png(outfilename, height=2000, width=1000)
        R('''myplot( lraw, utrs )''')
        R['dev.off']()

        # plot scaled data
        fn = ".".join((parts[0], parts[4], "scaled", "png"))
        outfilename = os.path.join(outdir, fn)

        R.png(outfilename, height=2000, width=1000)
        R('''myplot( lscaled, utrs )''')
        R['dev.off']()

    P.touch(outfile)


def filterAndMergeGTF(infile, outfile, remove_genes, merge=False):
    '''filter gtf file infile with gene ids in remove_genes
    and write to outfile.

    If *merge* is set, the resultant transcript models are merged by overlap.

    A summary file "<outfile>.summary" contains the number of
    transcripts that failed various filters.

    A file "<outfile>.removed.tsv.gz" contains the filters that a
    transcript failed.

    '''

    counter = E.Counter()

    # write summary table
    outf = IOTools.openFile(outfile + ".removed.tsv.gz", "w")
    outf.write("gene_id\tnoverlap\tsection\n")
    for gene_id, r in remove_genes.iteritems():
        for s in r:
            counter[s] += 1
        outf.write("%s\t%i\t%s\n" % (gene_id,
                                     len(r),
                                     ",".join(r)))
    outf.close()

    # filter gtf file
    tmpfile = P.getTempFile(".")
    inf = GTF.iterator(IOTools.openFile(infile))

    genes_input, genes_output = set(), set()

    for gtf in inf:
        genes_input.add(gtf.gene_id)
        if gtf.gene_id in remove_genes:
            continue
        genes_output.add(gtf.gene_id)
        tmpfile.write("%s\n" % str(gtf))

    tmpfile.close()
    tmpfilename = tmpfile.name

    outf = IOTools.openFile(outfile + ".summary.tsv.gz", "w")
    outf.write("category\ttranscripts\n")
    for x, y in counter.iteritems():
        outf.write("%s\t%i\n" % (x, y))
    outf.write("input\t%i\n" % len(genes_input))
    outf.write("output\t%i\n" % len(genes_output))
    outf.write("removed\t%i\n" % (len(genes_input) - len(genes_output)))

    outf.close()

    # close-by exons need to be merged, otherwise
    # cuffdiff fails for those on "." strand

    if merge:
        statement = '''
        %(pipeline_scriptsdir)s/gff_sort pos < %(tmpfilename)s
        | python %(scriptsdir)s/gtf2gtf.py
            --method=unset-genes --pattern-identifier="NONC%%06i"
            --log=%(outfile)s.log
        | python %(scriptsdir)s/gtf2gtf.py
            --method=merge-genes
            --log=%(outfile)s.log
        | python %(scriptsdir)s/gtf2gtf.py
            --method=merge-exons
            --merge-exons-distance=5
            --log=%(outfile)s.log
        | python %(scriptsdir)s/gtf2gtf.py
            --method=renumber-genes --pattern-identifier="NONC%%06i"
            --log=%(outfile)s.log
        | python %(scriptsdir)s/gtf2gtf.py
            --method=renumber-transcripts --pattern-identifier="NONC%%06i"
            --log=%(outfile)s.log
        | %(pipeline_scriptsdir)s/gff_sort genepos
        | gzip > %(outfile)s
        '''
    else:
        statement = '''
        %(pipeline_scriptsdir)s/gff_sort pos < %(tmpfilename)s
        | gzip > %(outfile)s
        '''

    P.run()

    os.unlink(tmpfilename)


#############################################################
#############################################################
#############################################################
# running cufflinks
#############################################################
def runCufflinks(infiles, outfile):
    '''estimate expression levels in each set.
    '''

    gtffile, bamfile = infiles

    job_threads = PARAMS["cufflinks_threads"]

    track = os.path.basename(P.snip(gtffile, ".gtf.gz"))

    tmpfilename = P.getTempFilename(".")
    if os.path.exists(tmpfilename):
        os.unlink(tmpfilename)

    gtffile = os.path.abspath(gtffile)
    bamfile = os.path.abspath(bamfile)
    outfile = os.path.abspath(outfile)

    # note: cufflinks adds \0 bytes to gtf file - replace with '.'
    # increase max-bundle-length to 4.5Mb due to Galnt-2 in mm9 with a 4.3Mb
    # intron.

    # AH: removed log messages about BAM record error
    # These cause logfiles to grow several Gigs and are
    # frequent for BAM files not created by tophat.
    # Error is:
    # BAM record error: found spliced alignment without XS attribute
    statement = '''mkdir %(tmpfilename)s;
    cd %(tmpfilename)s;
    cufflinks --label %(track)s
              --GTF <(gunzip < %(gtffile)s)
              --num-threads %(cufflinks_threads)i
              --frag-bias-correct %(bowtie_index_dir)s/%(genome)s.fa
              --library-type %(cufflinks_library_type)s
              %(cufflinks_options)s
              %(bamfile)s
    | grep -v 'BAM record error'
    >& %(outfile)s;
    perl -p -e "s/\\0/./g" < transcripts.gtf | gzip > %(outfile)s.gtf.gz;
    gzip < isoforms.fpkm_tracking > %(outfile)s.fpkm_tracking.gz;
    gzip < genes.fpkm_tracking > %(outfile)s.genes_tracking.gz;
    '''

    P.run()

    shutil.rmtree(tmpfilename)


def loadCufflinks(infile, outfile):
    '''load expression level measurements.'''

    track = P.snip(outfile, ".load")
    P.load(infile + ".genes_tracking.gz",
           outfile=track + "_genefpkm.load",
           options="--add-index=gene_id "
           "--ignore-column=tracking_id "
           "--ignore-column=class_code "
           "--ignore-column=nearest_ref_id")

    track = P.snip(outfile, ".load")
    P.load(infile + ".fpkm_tracking.gz",
           outfile=track + "_fpkm.load",
           options="--add-index=tracking_id "
           "--ignore-column=nearest_ref_id "
           "--rename-column=tracking_id:transcript_id")

    P.touch(outfile)


def mergeCufflinksFPKM(infiles, outfile,
                       tracking="genes_tracking",
                       identifier="gene_id"):
    '''build aggregate table with cufflinks FPKM values.

    * tracking* - select file type to merge:
    genes_tracking: genes
    fpkm_tracking: isoforms
    '''

    prefix = os.path.basename(outfile)
    prefix = prefix[:prefix.index("_")]
    print prefix

    headers = ",".join(
        [re.match("fpkm.dir/.*_(.*).cufflinks", x).groups()[0]
         for x in infiles])

    statement = '''
    python %(scriptsdir)s/combine_tables.py
        --log=%(outfile)s.log
        --columns=1
        --skip-titles
        --header-names=%(headers)s
        --take=FPKM fpkm.dir/%(prefix)s_*.%(tracking)s.gz
    | perl -p -e "s/tracking_id/%(identifier)s/"
    | %(pipeline_scriptsdir)s/hsort 1
    | gzip
    > %(outfile)s
    '''
    P.run()


def runFeatureCounts(annotations_file,
                     bamfile,
                     outfile,
                     nthreads=4,
                     strand=0,
                     options=""):
    '''run feature counts on *annotations_file* with
    *bam_file*.

    If the bam-file is paired, paired-end counting
    is enabled and the bam file automatically sorted.
    '''

    # featureCounts cannot handle gzipped in or out files
    outfile = P.snip(outfile, ".gz")
    tmpdir = P.getTempDir()
    annotations_tmp = os.path.join(tmpdir,
                                   'geneset.gtf')
    bam_tmp = os.path.join(tmpdir,
                           os.path.basename(bamfile))

    # -p -B specifies count fragments rather than reads, and both
    # reads must map to the feature
    # for legacy reasons look at feature_counts_paired
    if BamTools.isPaired(bamfile):
        # select paired end mode, additional options
        paired_options = "-p -B"
        # remove .bam extension
        bam_prefix = P.snip(bam_tmp, ".bam")
        # sort by read name
        paired_processing = \
            """samtools
            sort -@ %(nthreads)i -n %(bamfile)s %(bam_prefix)s;
            checkpoint; """ % locals()
        bamfile = bam_tmp
    else:
        paired_options = ""
        paired_processing = ""

    job_threads = nthreads

    # AH: what is the -b option doing?
    statement = '''mkdir %(tmpdir)s;
                   zcat %(annotations_file)s > %(annotations_tmp)s;
                   checkpoint;
                   %(paired_processing)s
                   featureCounts %(options)s
                                 -T %(nthreads)i
                                 -s %(strand)s
                                 -a %(annotations_tmp)s
                                 %(paired_options)s
                                 -o %(outfile)s
                                 %(bamfile)s
                    >& %(outfile)s.log;
                    checkpoint;
                    gzip -f %(outfile)s;
                    checkpoint;
                    rm -rf %(tmpdir)s
    '''

    P.run()


def buildExpressionStats(dbhandle, tables, method, outfile, outdir):
    '''build expression summary statistics.

    Creates also diagnostic plots in

    <exportdir>/<method> directory.
    '''
    def _split(tablename):
        # this would be much easier, if feature_counts/gene_counts/etc.
        # would not contain an underscore.
        try:
            design, geneset, counting_method = re.match(
                "([^_]+)_vs_([^_]+)_(.*)_%s" % method,
                tablename).groups()
        except AttributeError:
            try:
                design, geneset = re.match(
                    "([^_]+)_([^_]+)_%s" % method,
                    tablename).groups()
                print design, geneset
                counting_method = "na"
            except AttributeError:
                raise ValueError("can't parse tablename %s" % tablename)

        return design, geneset, counting_method

        # return re.match("([^_]+)_", tablename ).groups()[0]

    keys_status = "OK", "NOTEST", "FAIL", "NOCALL"

    outf = IOTools.openFile(outfile, "w")
    outf.write("\t".join(
        ("design",
         "geneset",
         "level",
         "counting_method",
         "treatment_name",
         "control_name",
         "tested",
         "\t".join(["status_%s" % x for x in keys_status]),
         "significant",
         "twofold")) + "\n")

    all_tables = set(Database.getTables(dbhandle))

    for level in CUFFDIFF_LEVELS:

        for tablename in tables:

            tablename_diff = "%s_%s_diff" % (tablename, level)
            tablename_levels = "%s_%s_diff" % (tablename, level)
            design, geneset, counting_method = _split(tablename_diff)
            if tablename_diff not in all_tables:
                continue

            def toDict(vals, l=2):
                return collections.defaultdict(
                    int,
                    [(tuple(x[:l]), x[l]) for x in vals])

            tested = toDict(
                Database.executewait(
                    dbhandle,
                    "SELECT treatment_name, control_name, "
                    "COUNT(*) FROM %(tablename_diff)s "
                    "GROUP BY treatment_name,control_name" % locals()
                    ).fetchall())
            status = toDict(Database.executewait(
                dbhandle,
                "SELECT treatment_name, control_name, status, "
                "COUNT(*) FROM %(tablename_diff)s "
                "GROUP BY treatment_name,control_name,status"
                % locals()).fetchall(), 3)
            signif = toDict(Database.executewait(
                dbhandle,
                "SELECT treatment_name, control_name, "
                "COUNT(*) FROM %(tablename_diff)s "
                "WHERE significant "
                "GROUP BY treatment_name,control_name" % locals()
                ).fetchall())

            fold2 = toDict(Database.executewait(
                dbhandle,
                "SELECT treatment_name, control_name, "
                "COUNT(*) FROM %(tablename_diff)s "
                "WHERE (l2fold >= 1 or l2fold <= -1) AND significant "
                "GROUP BY treatment_name,control_name,significant"
                % locals()).fetchall())

            for treatment_name, control_name in tested.keys():
                outf.write("\t".join(map(str, (
                    design,
                    geneset,
                    level,
                    counting_method,
                    treatment_name,
                    control_name,
                    tested[(treatment_name, control_name)],
                    "\t".join(
                        [str(status[(treatment_name, control_name, x)])
                         for x in keys_status]),
                    signif[(treatment_name, control_name)],
                    fold2[(treatment_name, control_name)]))) + "\n")

            ###########################################
            ###########################################
            ###########################################
            # plot length versus P-Value
            data = Database.executewait(
                dbhandle,
                "SELECT i.sum, pvalue "
                "FROM %(tablename_diff)s, "
                "%(geneset)s_geneinfo as i "
                "WHERE i.gene_id = test_id AND "
                "significant" % locals()).fetchall()

            # require at least 10 datapoints - otherwise smooth scatter fails
            if len(data) > 10:
                data = zip(*data)

                pngfile = ("%(outdir)s/%(design)s_%(geneset)s_%(level)s"
                           "_pvalue_vs_length.png") % locals()
                R.png(pngfile)
                R.smoothScatter(R.log10(ro.FloatVector(data[0])),
                                R.log10(ro.FloatVector(data[1])),
                                xlab='log10( length )',
                                ylab='log10( pvalue )',
                                log="x", pch=20, cex=.1)

                R['dev.off']()

    outf.close()


def parseCuffdiff(infile, min_fpkm=1.0):
    '''parse a cuffdiff .diff output file.'''

    CuffdiffResult = collections.namedtuple(
        "CuffdiffResult",
        "test_id gene_id gene  locus   sample_1 sample_2  "
        " status  value_1 value_2 l2fold  "
        "test_stat p_value q_value significant ")

    results = []

    for line in IOTools.openFile(infile):
        if line.startswith("test_id"):
            continue
        data = CuffdiffResult._make(line[:-1].split("\t"))
        status = data.status
        significant = [0, 1][data.significant == "yes"]
        if status == "OK" and (float(data.value_1) < min_fpkm or
                               float(data.value_2) < min_fpkm):
            status = "NOCALL"

        try:
            fold = math.pow(2.0, float(data.l2fold))
        except OverflowError:
            fold = "na"

        results.append(Expression.GeneExpressionResult._make((
            data.test_id,
            data.sample_1,
            data.value_1,
            0,
            data.sample_2,
            data.value_2,
            0,
            data.p_value,
            data.q_value,
            data.l2fold,
            fold,
            data.l2fold,
            significant,
            status)))

    return results


def loadCuffdiff(infile, outfile, min_fpkm=1.0):
    '''load results from differential expression analysis and produce
    summary plots.

    Note: converts from ln(fold change) to log2 fold change.

    The cuffdiff output is parsed.

    Pairwise comparisons in which one gene is not expressed (fpkm <
    fpkm_silent) are set to status 'NOCALL'. These transcripts might
    nevertheless be significant.

    This requires the cummeRbund library to be present in R.

    '''

    prefix = P.toTable(outfile)
    indir = infile + ".dir"

    if not os.path.exists(indir):
        P.touch(outfile)
        return

    # E.info( "building cummeRbund database" )
    # R('''library(cummeRbund)''')
    # cuff = R('''readCufflinks(dir = %(indir)s, dbfile=%(indir)s/csvdb)''' )
    # to be continued

    dbhandle = sqlite3.connect(PARAMS["database"])

    tmpname = P.getTempFilename(".")

    # ignore promoters and splicing - no fold change column, but  sqrt(JS)
    for fn, level in (("cds_exp.diff.gz", "cds"),
                      ("gene_exp.diff.gz", "gene"),
                      ("isoform_exp.diff.gz", "isoform"),
                      # ("promoters.diff.gz", "promotor"),
                      # ("splicing.diff.gz", "splice"),
                      ("tss_group_exp.diff.gz", "tss")):

        tablename = prefix + "_" + level + "_diff"

        infile = os.path.join(indir, fn)
        results = parseCuffdiff(infile,
                                min_fpkm=min_fpkm)

        Expression.writeExpressionResults(tmpname, results)

        statement = '''cat %(tmpname)s
        | python %(scriptsdir)s/csv2db.py %(csv2db_options)s
              --allow-empty-file
              --add-index=treatment_name
              --add-index=control_name
              --add-index=test_id
              --table=%(tablename)s
         >> %(outfile)s.log
         '''

        P.run()

    for fn, level in (("cds.fpkm_tracking.gz", "cds"),
                      ("genes.fpkm_tracking.gz", "gene"),
                      ("isoforms.fpkm_tracking.gz", "isoform"),
                      ("tss_groups.fpkm_tracking.gz", "tss")):

        tablename = prefix + "_" + level + "_levels"

        statement = '''zcat %(indir)s/%(fn)s
        | python %(scriptsdir)s/csv2db.py %(csv2db_options)s
              --allow-empty-file
              --add-index=tracking_id
              --table=%(tablename)s
         >> %(outfile)s.log
         '''

        P.run()

    # Jethro - load tables of sample specific cuffdiff fpkm values into csvdb
    # IMS: First read in lookup table for CuffDiff/Pipeline sample name
    # conversion
    inf = IOTools.openFile(os.path.join(indir, "read_groups.info.gz"))
    inf.readline()
    sample_lookup = {}

    for line in inf:
        line = line.split("\t")
        our_sample_name = IOTools.snip(line[0])
        our_sample_name = re.sub("-", "_", our_sample_name)
        cuffdiff_sample_name = "%s_%s" % (line[1], line[2])
        sample_lookup[cuffdiff_sample_name] = our_sample_name

    inf.close()

    for fn, level in (("cds.read_group_tracking.gz", "cds"),
                      ("genes.read_group_tracking.gz", "gene"),
                      ("isoforms.read_group_tracking.gz", "isoform"),
                      ("tss_groups.read_group_tracking.gz", "tss")):

        tablename = prefix + "_" + level + "sample_fpkms"

        tmpf = P.getTempFilename(".")
        inf = IOTools.openFile(os.path.join(indir, fn)).readlines()
        outf = IOTools.openFile(tmpf, "w")

        samples = []
        genes = {}

        x = 0
        for line in inf:
            if x == 0:
                x += 1
                continue
            line = line.split()
            gene_id = line[0]
            condition = line[1]
            replicate = line[2]
            fpkm = line[6]
            status = line[8]

            sample_id = condition + "_" + replicate

            if sample_id not in samples:
                samples.append(sample_id)

            # IMS: The following block keeps getting its indenting messed
            # up. It is not part of the 'if sample_id not in samples' block
            # plesae make sure it does not get made part of it
            if gene_id not in genes:
                genes[gene_id] = {}
                genes[gene_id][sample_id] = fpkm
            else:
                if sample_id in genes[gene_id]:
                    raise ValueError(
                        'sample_id %s appears twice in file for gene_id %s'
                        % (sample_id, gene_id))
                else:
                    if status != "OK":
                        genes[gene_id][sample_id] = status
                    else:
                        genes[gene_id][sample_id] = fpkm

        samples = sorted(samples)

        # IMS - CDS files might be empty if not cds has been
        # calculated for the genes in the long term need to add CDS
        # annotation to denovo predicted genesets in meantime just
        # skip if cds tracking file is empty

        if len(samples) == 0:
            continue

        headers = "gene_id\t" + "\t".join([sample_lookup[x] for x in samples])
        outf.write(headers + "\n")

        for gene in genes.iterkeys():
            outf.write(gene + "\t")
            x = 0
            while x < len(samples) - 1:
                outf.write(genes[gene][samples[x]] + "\t")
                x += 1

            # IMS: Please be careful with this line. It keeps getting moved
            # into the above while block where it does not belong
            outf.write(genes[gene][samples[len(samples) - 1]] + "\n")

        outf.close()

        statement = ("cat %(tmpf)s |"
                     " python %(scriptsdir)s/csv2db.py "
                     "  %(csv2db_options)s"
                     "  --allow-empty-file"
                     "  --add-index=gene_id"
                     "  --table=%(tablename)s"
                     " >> %(outfile)s.log")
        P.run()

        os.unlink(tmpf)

    # build convenience table with tracks
    tablename = prefix + "_isoform_levels"
    tracks = Database.getColumnNames(dbhandle, tablename)
    tracks = [x[:-len("_FPKM")] for x in tracks if x.endswith("_FPKM")]

    tmpfile = P.getTempFile(dir=".")
    tmpfile.write("track\n")
    tmpfile.write("\n".join(tracks) + "\n")
    tmpfile.close()

    P.load(tmpfile.name, outfile)
    os.unlink(tmpfile.name)


def runCuffdiff(bamfiles,
                design_file,
                geneset_file,
                outfile,
                cuffdiff_options="",
                threads=4,
                fdr=0.1,
                mask_file=None):
    '''estimate differential expression using cuffdiff.

    infiles
       bam files

    geneset_file
       geneset to use for the analysis

    design_file
       design file describing which differential expression to test

    Replicates within each track are grouped.
    '''

    design = Expression.readDesignFile(design_file)

    outdir = outfile + ".dir"
    try:
        os.mkdir(outdir)
    except OSError:
        pass

    job_threads = threads

    # replicates are separated by ","
    reps = collections.defaultdict(list)
    for bamfile in bamfiles:
        groups = collections.defaultdict()
        # .accepted.bam kept for legacy reasons (see rnaseq pipeline)
        track = P.snip(os.path.basename(bamfile), ".bam", ".accepted.bam")
        if track not in design:
            E.warn("bamfile '%s' not part of design - skipped" % bamfile)
            continue

        d = design[track]
        if not d.include:
            continue
        reps[d.group].append(bamfile)

    groups = sorted(reps.keys())
    labels = ",".join(groups)
    reps = "   ".join([",".join(reps[group]) for group in groups])

    # Nick - add mask gtf to not assess rRNA and ChrM
    extra_options = []

    if mask_file:
        extra_options.append(" -M %s" % os.path.abspath(mask_file))

    extra_options = " ".join(extra_options)

    # IMS added a checkpoint to catch cuffdiff errors
    # AH: removed log messages about BAM record error
    # These cause logfiles to grow several Gigs and are
    # frequent for BAM files not created by tophat.
    # Error is:
    # BAM record error: found spliced alignment without XS attribute
    # AH: compress output in outdir
    statement = '''date > %(outfile)s.log;
    hostname >> %(outfile)s.log;
    cuffdiff --output-dir %(outdir)s
             --verbose
             --num-threads %(threads)i
             --labels %(labels)s
             --FDR %(fdr)f
             %(extra_options)s
             %(cuffdiff_options)s
             <(gunzip < %(geneset_file)s )
             %(reps)s
    2>&1
    | grep -v 'BAM record error'
    >> %(outfile)s.log;
    checkpoint;
    gzip -f %(outdir)s/*;
    checkpoint;
    date >> %(outfile)s.log;
    '''
    P.run()

    results = parseCuffdiff(os.path.join(outdir, "gene_exp.diff.gz"))

    Expression.writeExpressionResults(outfile, results)
