import os
import sys
import re
import types
import itertools

from CGATReport.Tracker import *
from collections import OrderedDict as odict
from exomeReport import *


class Snp(ExomeTracker):

    pattern = "(_?\S{5,6})_mutect_snp_annotated_tsv$"

    def __call__(self, track, slice=None):

        statement = '''
        SELECT A.CHROM AS Chr, A.POS AS Pos,
        A.SNPEFF_GENE_NAME AS Gene,
        A.SNPEFF_EXON_ID AS Exon,
        A.REF, A.ALT,
        A.SNPEFF_IMPACT AS Impact, A.SNPEFF_GENE_BIOTYPE AS Biotype,
        SNPEFF_AMINO_ACID_CHANGE AS AA_change,
        SNPEFF_CODON_CHANGE AS Codon_change,
        C.type as NCG, C.cancer_type, D.*,
        B.n_ref_count AS Normal_Ref, B.n_alt_count AS Normal_Alt,
        B.t_ref_count AS Tumor_Ref, B.t_alt_count AS Tumor_Alt
        FROM %(track)s_mutect_snp_annotated_tsv AS A
        JOIN %(track)s_call_stats_out AS B
        ON A.CHROM = B.contig AND A.POS = B.position
        LEFT OUTER JOIN cancergenes as C
        ON A.SNPEFF_GENE_NAME = C.symbol
        LEFT OUTER JOIN eBio_studies_gene_frequencies as D
        ON A.SNPEFF_GENE_NAME = D.gene
        WHERE A.FILTER!='REJECT'
        AND B.t_alt_count > 3
        AND (1.0*B.n_alt_count)/(B.n_ref_count + B.n_alt_count) < 0.03
        AND (1.0*B.t_alt_count)/(B.t_ref_count + B.t_alt_count) > 0.06
        AND (B.n_ref_count + B.n_alt_count) > 19;
        ''' % locals()
        return self.getAll(statement)


class Indel(ExomeTracker):

    pattern = "(_?\S{5,6})_indels_annotated_tsv$"

    def __call__(self, track, slice=None):

        statement = '''
        SELECT A.CHROM AS Chr, A.POS AS Pos,
        A.SNPEFF_GENE_NAME AS Gene,
        A.SNPEFF_EXON_ID AS Exon,
        A.REF, A.ALT,
        A.SNPEFF_IMPACT AS Impact, A.SNPEFF_GENE_BIOTYPE AS Biotype,
        A.SNPEFF_AMINO_ACID_CHANGE AS AA_change,
        A.SNPEFF_CODON_CHANGE AS Codon_change,
        B.type as NCG, B.cancer_type,  C.*,
        A.NORMAL_DP AS Normal_depth,
        A.TUMOR_DP AS Tumor_depth,
        A.NORMAL_TAR as Normal_Ref, A.NORMAL_TIR as Normal_Alt,
        A.TUMOR_TAR as Tumor_Ref, A.TUMOR_TIR as Tumor_Alt
        FROM %(track)s_indels_annotated_tsv AS A
        LEFT OUTER JOIN cancergenes as B
        ON A.SNPEFF_GENE_NAME = B.symbol
        LEFT OUTER JOIN eBio_studies_gene_frequencies as C
        ON A.SNPEFF_GENE_NAME = C.gene
        WHERE A.QSI_NT > 20 AND A.IHP < 12
        AND A.RC < 12 AND A.IC < 12;
        ''' % locals()

        return self.getAll(statement)


class FilterSummary(ExomeTracker):

    pattern = "(_?\S{5,6})_mutect_filtering_summary$"

    def __call__(self, track, slice=None):

        statement = '''
        SELECT * FROM %(track)s_mutect_filtering_summary
        ;
        ''' % locals()

        return self.getAll(statement)
