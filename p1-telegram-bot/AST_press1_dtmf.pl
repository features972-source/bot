#!/usr/bin/perl
use strict;
use warnings;
use IO::Socket::INET;

my $host = '127.0.0.1';
my $port = 5038;
my $user = 'cron';
my $pass = '1234';
my $log  = '/var/log/astguiclient/press1_dtmf.log';
my $out  = '/var/lib/asterisk/press1_dtmf_events.jsonl';

sub logmsg { my ($m)=@_; open my $fh,'>>',$log or return; print $fh scalar(localtime)." $m\n"; close $fh; }

sub emit_event {
    my (%ev) = @_;
    my @parts;
    for my $k (sort keys %ev) {
        my $v = $ev{$k} // '';
        $v =~ s/\\/\\\\/g;
        $v =~ s/"/\\"/g;
        push @parts, qq("$k":"$v");
    }
    open my $fh, '>>', $out or return;
    print $fh '{', join(',', @parts), "}\n";
    close $fh;
}

sub ami_send { my ($s,$a,%f)=@_; print $s "Action: $a\r\n"; print $s "$_: $f{$_}\r\n" for sort keys %f; print $s "\r\n"; }

sub outbound_bitcall {
    my ($ch) = @_;
    return 0 unless defined $ch && $ch =~ /^PJSIP\/bitcall-/i;
    return 0 if $ch =~ /3cx/i;
    return 1;
}

my %recent_xfer;
my %digits;
my %lead_cache;
my %chan_ctx;
my %chan_app;
my %chan_ext;

sub ivr_active {
    my ($chan) = @_;
    my $ctx = $chan_ctx{$chan} // '';
    return 0 unless $ctx eq 'press1-ivr';
    my $app = $chan_app{$chan} // '';
    return 0 if $app =~ /Dial/i;
    my $ext = $chan_ext{$chan} // '';
    return 0 if $ext =~ /^(?:xfer|xferdial|hang|1)$/i;
    return 1;
}

sub try_xfer_on_one {
    my ($sock, $chan) = @_;
    my $now = time();
    return if $recent_xfer{$chan} && ($now - $recent_xfer{$chan}) < 3;
    return unless ivr_active($chan);

    $recent_xfer{$chan} = $now;
    logmsg("DTMF 1 on $chan (ivr) -> press1-ivr,1,1");
    ami_send(
        $sock, 'Redirect',
        Channel  => $chan,
        Context  => 'press1-ivr',
        Exten    => '1',
        Priority => '1',
    );
    # Do not read AMI response here — mixed reads corrupt the event loop.
    logmsg("Redirect sent for $chan");
}

while (1) {
    my $sock = IO::Socket::INET->new(PeerAddr=>$host, PeerPort=>$port, Proto=>'tcp', Timeout=>10);
    unless ($sock) { logmsg("AMI connect failed: $!"); sleep 5; next; }
    ami_send($sock, 'Login', Username=>$user, Secret=>$pass, Events=>'call,dtmf');
    my $buf = ''; my $li = 0;
    logmsg("AMI connected (call+dtmf)");
    while (my $line = <$sock>) {
        $buf .= $line;
        next unless $buf =~ /\r\n\r\n$/;
        my %ev;
        for my $l (split /\r\n/, $buf) {
            my ($k, $v) = split /: /, $l, 2;
            $ev{$k} = $v if defined $k && defined $v;
        }
        $buf = '';
        if (!$li && ($ev{Response}//'') eq 'Success' && ($ev{Message}//'') =~ /Authentication accepted/i) {
            $li = 1;
            logmsg("logged in");
            next;
        }

        my $evn = $ev{Event} // '';
        my $chan = $ev{Channel} // '';
        next unless outbound_bitcall($chan);

        if ($evn eq 'Newexten') {
            $chan_ctx{$chan} = $ev{Context} // $chan_ctx{$chan} // '';
            $chan_ext{$chan}  = $ev{Extension} // $chan_ext{$chan} // '';
            if (defined $ev{Application} && length $ev{Application}) {
                $chan_app{$chan} = $ev{Application};
            }
            my $ext = $ev{Extension} // '';
            $ext =~ s/\D//g;
            $lead_cache{$chan} = $ext if length($ext) >= 10;
            next;
        }

        if ($evn eq 'DTMFEnd') {
            my $digit = $ev{Digit} // '';
            next unless length $digit;

            try_xfer_on_one($sock, $chan) if $digit eq '1';

            my $lead = $lead_cache{$chan} // '';
            $lead =~ s/\D//g if $lead;
            $digits{$chan} //= '';
            $digits{$chan} .= $digit;
            emit_event(
                t    => int(time()),
                e    => 'digit',
                c    => $chan,
                lead => $lead,
                d    => $digit,
                seq  => $digits{$chan},
            );
            logmsg("captured $digit on $chan lead=$lead seq=$digits{$chan}");
            next;
        }

        if ($evn eq 'Hangup') {
            my $lead = $lead_cache{$chan} // '';
            $lead =~ s/\D//g if defined $lead;
            my $seq = $digits{$chan} // '';
            if (length $seq) {
                emit_event(
                    t      => int(time()),
                    e      => 'summary',
                    c      => $chan,
                    lead   => ($lead // ''),
                    digits => $seq,
                );
                logmsg("summary $chan lead=$lead digits=$seq");
            }
            delete $digits{$chan};
            delete $lead_cache{$chan};
            delete $recent_xfer{$chan};
            delete $chan_ctx{$chan};
            delete $chan_app{$chan};
            delete $chan_ext{$chan};
        }
    }
    logmsg("AMI disconnected");
    close $sock if $sock;
    sleep 2;
}
