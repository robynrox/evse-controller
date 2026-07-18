plugins {
    kotlin("jvm") version "1.9.22"
    application
}

group = "com.evse"
version = "0.1.0"

repositories {
    mavenCentral()
    google()
}

dependencies {
    // Kotlin standard library
    implementation(kotlin("stdlib"))
    
    // Modbus TCP library
    implementation("com.intelligt.modbus:jlibmodbus:1.2.9.11")
    
    // Logging
    implementation("ch.qos.logback:logback-classic:1.4.14")
    
    // Command-line argument parsing
    implementation("com.github.ajalt.clikt:clikt:4.2.0")
    
    // Testing
    testImplementation(kotlin("test"))
    testImplementation("org.junit.jupiter:junit-jupiter:5.10.1")
}

kotlin {
    jvmToolchain(21)
}

application {
    mainClass = "com.evse.WallboxControllerKt"
}

tasks.test {
    useJUnitPlatform()
}

tasks.jar {
    manifest {
        attributes["Main-Class"] = "com.evse.WallboxControllerKt"
    }
    
    // Include all dependencies for a fat JAR
    duplicatesStrategy = DuplicatesStrategy.EXCLUDE
    from(sourceSets.main.get().output)
    
    configurations.runtimeClasspath.get().forEach { dependency ->
        from(zipTree(dependency))
    }
}
