plugins {
    id 'java'
}

layout.buildDirectory = file('/tmp/quixbugs-build')

sourceSets {
    main.java.srcDirs = ['/app/src/main/java']
    test.java.srcDirs = ['/tests/java_testcases']
}

dependencies {
    testImplementation files(
        '/opt/quixbugs-verifier/junit-4.13.2.jar',
        '/opt/quixbugs-verifier/hamcrest-core-1.3.jar'
    )
}

test {
    maxHeapSize = '1024m'
    testLogging {
        events 'passed', 'skipped', 'failed'
        exceptionFormat 'full'
    }
}
